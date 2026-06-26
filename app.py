from flask import Flask, render_template, request, redirect, url_for, session, g, jsonify
from init_db import init_db
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
from datetime import datetime, timedelta, timezone, date
import zoneinfo
from collections import defaultdict
from functools import wraps
import sqlite3
import requests
import os
import unicodedata
from dotenv import load_dotenv
load_dotenv()
from flask_babel import Babel, gettext as _
from mailer import send_email
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY não configurada — define a variável de ambiente.")

# Inicializar DB automaticamente ao arrancar
init_db()
API_FOOTBALL_BASE_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")
if not API_FOOTBALL_KEY:
    raise RuntimeError("API_FOOTBALL_KEY não configurada — define a variável de ambiente.")
WORLD_CUP_LEAGUE_ID = 1
#ajustar para 2026 quando comprar
WORLD_CUP_SEASON = 2026
serializer = URLSafeTimedSerializer(app.secret_key)

# =========================
# BABEL — INTERNACIONALIZAÇÃO
# Suporta: pt (PT-PT), pt_BR (PT-BR), en (Inglês)
# =========================
app.config['BABEL_DEFAULT_LOCALE'] = 'pt'
app.config['BABEL_DEFAULT_TIMEZONE'] = 'Europe/Lisbon'
app.config['LANGUAGES'] = ['pt', 'pt_br', 'en']

# Compila .po -> .mo no boot. Em produção (Railway) o passo de build não
# garante .mo presentes, e .mo é o que o Flask-Babel lê em runtime.
def _compile_translations():
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'translations')
    for loc in os.listdir(root):
        po_path = os.path.join(root, loc, 'LC_MESSAGES', 'messages.po')
        mo_path = os.path.join(root, loc, 'LC_MESSAGES', 'messages.mo')
        if not os.path.exists(po_path):
            continue
        if os.path.exists(mo_path) and os.path.getmtime(mo_path) >= os.path.getmtime(po_path):
            continue
        with open(po_path, 'rb') as f:
            catalog = read_po(f)
        with open(mo_path, 'wb') as f:
            write_mo(f, catalog)
        print(f'[babel] compiled {loc} -> {mo_path}', flush=True)

_compile_translations()

babel = Babel()

def get_locale():
    """Detecta o idioma da sessão do utilizador."""
    lang = session.get('lang')
    if lang and lang in app.config['LANGUAGES']:
        return lang
    return 'pt'

babel.init_app(app, locale_selector=get_locale)

# Garante que {% trans %} usa o mesmo locale que _()
from flask_babel import get_translations
app.jinja_env.install_gettext_callables(
    lambda x: get_translations().ugettext(x),
    lambda s, p, n: get_translations().ungettext(s, p, n),
    newstyle=True
)





# =========================
# PROTECÇÃO BRUTE FORCE
# Máximo 5 tentativas de login por IP em 15 minutos
# Guarda em memória — reinicia com o servidor
# =========================
login_attempts = defaultdict(list)
MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

def is_rate_limited(ip):
    """Verifica se o IP está bloqueado por demasiadas tentativas."""
    now = datetime.now()
    cutoff = now - timedelta(minutes=LOCKOUT_MINUTES)
    # Limpar tentativas antigas
    login_attempts[ip] = [t for t in login_attempts[ip] if t > cutoff]
    return len(login_attempts[ip]) >= MAX_ATTEMPTS

def register_failed_attempt(ip):
    """Regista uma tentativa falhada para este IP."""
    login_attempts[ip].append(datetime.now())

def clear_attempts(ip):
    """Limpa as tentativas após login bem sucedido."""
    login_attempts[ip] = []

# =========================
# CONEXÃO COM O BANCO
# =========================
def get_db_connection():
    conn = sqlite3.connect(os.environ.get("DATABASE_PATH", "database.db"))
    conn.row_factory = sqlite3.Row
    return conn


# timezone preferido por país — BR vê em horário de São Paulo, resto em Lisboa
def user_timezone(country_code):
    if country_code == "BR":
        return zoneinfo.ZoneInfo("America/Sao_Paulo")
    return zoneinfo.ZoneInfo("Europe/Lisbon")


# país é derivado da unidade Eureka — fonte única de verdade.
# Evita combinações incoerentes (ex.: unidade São Paulo com bandeira de Portugal).
UNIT_COUNTRY = {
    "lisboa": "PT",
    "campinas": "BR",
    "sao_paulo": "BR",
}


def country_from_unit(eureka_unit):
    return UNIT_COUNTRY.get(eureka_unit)


# achar jogo por nome do time
def find_db_game_by_team_names(conn, home_name, away_name):
    """Procura jogo na DB pelos nomes dos times, em qualquer ordem.

    Retorna (row, swapped) onde swapped=True se a DB tem os times
    invertidos em relação à API. O caller deve usar swapped para
    flipar score_home/score_away antes de gravar.
    """
    rows = conn.execute("""
        SELECT
            g.id,
            g.api_game_id,
            th.name AS home_name,
            ta.name AS away_name
        FROM games g
        LEFT JOIN teams th ON g.team_home_id = th.id
        LEFT JOIN teams ta ON g.team_away_id = ta.id
    """).fetchall()

    api_home = normalize_team_name(home_name)
    api_away = normalize_team_name(away_name)

    for row in rows:
        db_home = normalize_team_name(row["home_name"])
        db_away = normalize_team_name(row["away_name"])

        if db_home == api_home and db_away == api_away:
            return row, False
        if db_home == api_away and db_away == api_home:
            return row, True

    return None, False

# funcão principal de sincronização

def sync_games_from_api():
    fixtures = fetch_world_cup_fixtures()
    conn = get_db_connection()

    matched_games = 0
    updated_games = 0
    inserted_games = 0
    skipped_games = 0
    skipped_details = []

    # Mapeamento de stage da API para stage_id na DB
    # A API usa nomes em inglês para as rondas
    STAGE_NAME_MAP = {
        "Group Stage": 1,
        "2nd Round": 2,
        "Round of 16": 3,
        "Quarter-finals": 4,
        "Semi-finals": 5,
        "3rd Place Final": 6,
        "Final": 7,
        "Round of 32": 2,
    }

    for item in fixtures:
        fixture = item.get("fixture", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})
        league = item.get("league", {})

        fixture_id = fixture.get("id")
        fixture_date = fixture.get("date")

        home_name = teams.get("home", {}).get("name")
        away_name = teams.get("away", {}).get("name")

        # O campo `goals` da API traz o placar AO VIVO durante a partida.
        # Só consideramos resultado quando o jogo realmente terminou —
        # senão um 0x0 de jogo em andamento marcaria o jogo como encerrado
        # e ainda pontuaria no ranking antes da hora.
        status_short = fixture.get("status", {}).get("short")
        score_obj = item.get("score", {})
        FINISHED_STATUSES = {"FT", "AET", "PEN"}

        if status_short in FINISHED_STATUSES:
            if status_short == "PEN":
                # Usar resultado do extratime (90min + prorrogação)
                et = score_obj.get("extratime", {})
                score_home = et.get("home")
                score_away = et.get("away")
                if score_home is None:
                    score_home = goals.get("home")
                    score_away = goals.get("away")
                # Quem venceu nos pênaltis
                pen = score_obj.get("penalty", {})
                pen_home = pen.get("home")
                pen_away = pen.get("away")
                penalty_winner_api_side = "home" if (pen_home or 0) > (pen_away or 0) else "away"
            else:
                score_home = goals.get("home")
                score_away = goals.get("away")
                penalty_winner_api_side = None
        else:
            score_home = None
            score_away = None
            penalty_winner_api_side = None

        # Nome da ronda na API
        round_name = league.get("round", "")

        if not fixture_id or not home_name or not away_name:
            skipped_games += 1
            skipped_details.append({
                "reason": "missing_data",
                "fixture_id": fixture_id,
                "home_name": home_name,
                "away_name": away_name
            })
            continue

        api_game_id = str(fixture_id)

        # tenta achar pelo api_game_id (trazendo os nomes pra calcular orientação)
        db_game = conn.execute("""
            SELECT g.id, g.api_game_id, th.name AS home_name, ta.name AS away_name
            FROM games g
            LEFT JOIN teams th ON g.team_home_id = th.id
            LEFT JOIN teams ta ON g.team_away_id = ta.id
            WHERE g.api_game_id = ?
        """, (api_game_id,)).fetchone()
        swapped = False

        # se não achou, tenta achar por nome dos times (unordered)
        if not db_game:
            db_game, swapped = find_db_game_by_team_names(conn, home_name, away_name)

        # se encontrou — actualizar (flipar scores se a base tem times em ordem oposta)
        if db_game:
            matched_games += 1

            # Orientação determinada SEMPRE pelos nomes — vale tanto pro match
            # por nome quanto por api_game_id. Antes, jogos casados por
            # api_game_id nunca eram flipados, invertendo o placar quando a
            # ordem da base divergia da ordem da API.
            db_home_norm = normalize_team_name(db_game["home_name"])
            db_away_norm = normalize_team_name(db_game["away_name"])
            api_home_norm = normalize_team_name(home_name)
            api_away_norm = normalize_team_name(away_name)
            if db_home_norm == api_away_norm and db_away_norm == api_home_norm:
                swapped = True
            elif db_home_norm == api_home_norm and db_away_norm == api_away_norm:
                swapped = False
            # se nenhuma orientação bate (nomes mudaram/TBC), mantém o swapped
            # vindo do match por nome.

            stored_home = score_away if swapped else score_home
            stored_away = score_home if swapped else score_away
         # Determinar penalty_winner_id
            penalty_winner_id = None
            if penalty_winner_api_side:
                if (penalty_winner_api_side == "home" and not swapped) or (penalty_winner_api_side == "away" and swapped):
                    penalty_winner_id = conn.execute("SELECT team_home_id FROM games WHERE id = ?", (db_game["id"],)).fetchone()["team_home_id"]
                else:
                    penalty_winner_id = conn.execute("SELECT team_away_id FROM games WHERE id = ?", (db_game["id"],)).fetchone()["team_away_id"]

            conn.execute("""
                UPDATE games
                SET
                    api_game_id = ?,
                    team_home_id = COALESCE(team_home_id, ?),
                    team_away_id = COALESCE(team_away_id, ?),
                    game_datetime = ?,
                    score_home = ?,
                    score_away = ?,
                    penalty_winner_id = ?
                WHERE id = ?
            """, (
                api_game_id,
                home_team["id"] if home_team else None,
                away_team["id"] if away_team else None,
                fixture_date,
                stored_home,
                stored_away,
                penalty_winner_id,
                db_game["id"]
            ))
            updated_games += 1
            continue

        # Não encontrou — tentar inserir (fases eliminatórias)
        # Determinar stage_id a partir do nome da ronda
        stage_id = None
        for stage_key, sid in STAGE_NAME_MAP.items():
            if stage_key.lower() in round_name.lower():
                stage_id = sid
                break

        if not stage_id:
            skipped_games += 1
            skipped_details.append({
                "reason": "unknown_stage",
                "fixture_id": fixture_id,
                "round_name": round_name,
                "home_name": home_name,
                "away_name": away_name
            })
            continue

        # Encontrar os ids das equipas na DB
        home_name_pt = TEAM_NAME_MAP.get(home_name, home_name)
        away_name_pt = TEAM_NAME_MAP.get(away_name, away_name)

        home_team = conn.execute(
            "SELECT id FROM teams WHERE name = ?", (home_name_pt,)
        ).fetchone()
        away_team = conn.execute(
            "SELECT id FROM teams WHERE name = ?", (away_name_pt,)
        ).fetchone()

        if not home_team or not away_team:
            skipped_games += 1
            skipped_details.append({
                "reason": "team_not_found",
                "fixture_id": fixture_id,
                "home_name": home_name,
                "away_name": away_name,
                "home_name_pt": home_name_pt,
                "away_name_pt": away_name_pt
            })
            continue

        # Inserir novo jogo
           # Verificar se existe um placeholder (sem equipas) para este stage e data
        fixture_date_only = fixture_date[:10] if fixture_date else None
        placeholder = None
        if fixture_date_only:
            placeholder = conn.execute("""
                SELECT id FROM games
                WHERE stage_id = ?
                  AND team_home_id IS NULL
                  AND team_away_id IS NULL
                  AND game_datetime LIKE ?
                LIMIT 1
            """, (stage_id, fixture_date_only + "%")).fetchone()

        if placeholder:
            # Actualizar o placeholder com as equipas e dados reais
            conn.execute("""
                UPDATE games
                SET
                    api_game_id = ?,
                    team_home_id = ?,
                    team_away_id = ?,
                    game_datetime = ?,
                    score_home = ?,
                    score_away = ?
                WHERE id = ?
            """, (
                api_game_id,
                home_team["id"],
                away_team["id"],
                fixture_date,
                score_home,
                score_away,
                placeholder["id"]
            ))
            updated_games += 1
        else:
            # Inserir novo jogo (não há placeholder)
            conn.execute("""
                INSERT INTO games (
                    api_game_id,
                    team_home_id,
                    team_away_id,
                    stage_id,
                    game_datetime,
                    score_home,
                    score_away
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                api_game_id,
                home_team["id"],
                away_team["id"],
                stage_id,
                fixture_date,
                score_home,
                score_away
            ))
            inserted_games += 1

    conn.commit()
    conn.close()

    return {
        "matched_games": matched_games,
        "updated_games": updated_games,
        "inserted_games": inserted_games,
        "skipped_games": skipped_games,
        "skipped_details": skipped_details[:20]
    }

# funcao de nomes
TEAM_NAME_MAP = {
    "Brazil": "Brasil",
    "Netherlands": "Países Baixos",
    "South Korea": "Coreia do Sul",
    "Saudi Arabia": "Arábia Saudita",
    "Switzerland": "Suíça",
    "Germany": "Alemanha",
    "Spain": "Espanha",
    "England": "Inglaterra",
    "Morocco": "Marrocos",
    "Croatia": "Croácia",
    "Japan": "Japão",
    "Tunisia": "Tunísia",
    "Mexico": "México",
    "Poland": "Polônia",
    "Belgium": "Bélgica",
    "Canada": "Canadá",
    "Cameroon": "Camarões",
    "Uruguay": "Uruguai",
    "Ghana": "Gana",
    "Serbia": "Sérvia",
    "Qatar": "Catar",
    "Ecuador": "Equador",
    "Iran": "Irã",
    "USA": "Estados Unidos",
    "Wales": "País de Gales",
    "Denmark": "Dinamarca",
    "Australia": "Austrália",
    "France": "França",
    "Argentina": "Argentina",
    "Portugal": "Portugal",
    "Senegal": "Senegal",
    "Costa Rica": "Costa Rica",
    "South Africa": "África do Sul",
    "Czech Republic": "República Tcheca",
    "Bosnia & Herzegovina": "Bósnia e Herzegovina",
    "Sweden": "Suécia",
    "Turkey": "Turquia",
    "DR Congo": "República Democrática do Congo",
    "Iraq": "Iraque",
    "South Korea": "Coreia do Sul",
    "Saudi Arabia": "Arábia Saudita",
    "Switzerland": "Suíça",
    "Germany": "Alemanha",
    "USA": "Estados Unidos",
    "Mexico": "México",
    "Belgium": "Bélgica",
    "Canada": "Canadá",
    "Uruguay": "Uruguai",
    "Ecuador": "Equador",
    "Qatar": "Catar",
    "Iran": "Irã",
    "Wales": "País de Gales",
    "Denmark": "Dinamarca",
    "Australia": "Austrália",
    "France": "França",
    "Netherlands": "Países Baixos",
    "Japan": "Japão",
    "Tunisia": "Tunísia",
    "Poland": "Polônia",
    "Cameroon": "Camarões",
    "Serbia": "Sérvia",
    "Ghana": "Gana",
    "Costa Rica": "Costa Rica",
    "Croatia": "Croácia",
    "Morocco": "Marrocos",
    "Portugal": "Portugal",
    "Senegal": "Senegal",
    "Argentina": "Argentina",
    "Spain": "Espanha",
    "England": "Inglaterra",
    "Norway": "Noruega",
    "Algeria": "Argélia",
    "Austria": "Áustria",
    "Jordan": "Jordânia",
    "Panama": "Panamá",
    "Colombia": "Colômbia",
    "Uzbekistan": "Uzbequistão",
    "Cape Verde": "Cabo Verde",
    "Ivory Coast": "Costa do Marfim",
    "New Zealand": "Nova Zelândia",
    "Egypt": "Egito",
    "Curacao": "Curaçao",
    "Haiti": "Haiti",
    "Paraguay": "Paraguai",
    "Scotland": "Escócia",
    # Aliases extras da API-Football que divergem do nome curto.
    # A API usa o nome oficial "Türkiye" e formas longas para alguns países.
    "Türkiye": "Turquia",
    "Cape Verde Islands": "Cabo Verde",
    "Congo DR": "República Democrática do Congo"
}

def normalize_team_name(name):
    if not name:
        return ""

    # NFC primeiro: garante que a chave do mapa case independentemente da
    # composição unicode (ex.: 'ü' precomposto U+00FC vs 'u' + combining ◌̈).
    name = unicodedata.normalize("NFC", name.strip())
    translated_name = TEAM_NAME_MAP.get(name, name)

    # Remove QUALQUER diacrítico genericamente (não só os da lista antiga),
    # então não depende de cada acento estar mapeado à mão.
    decomposed = unicodedata.normalize("NFKD", translated_name)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return without_accents.lower().strip()


# =========================
# fetch games /api-football
# =========================
def fetch_world_cup_fixtures():
    url = API_FOOTBALL_BASE_URL + "/fixtures"
    headers = {
        "x-apisports-key": API_FOOTBALL_KEY,
        "Accept": "application/json"
    }
    params = {
        "league": WORLD_CUP_LEAGUE_ID,
        "season": WORLD_CUP_SEASON
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)

    print("STATUS CODE:", response.status_code)
    print("FINAL URL:", response.url)
    print("RESPONSE TEXT:", response.text[:500])

    response.raise_for_status()

    data = response.json()
    return data.get("response", [])




# =========================
# AUDIT — lista pares (mesmo par de times no mesmo stage) duplicados
# Aceder em: /admin/games-audit (só admin, user_id=1)
# =========================
@app.route("/admin/games-audit")
def admin_games_audit():
    if session.get("user_id") != 1:
        return jsonify({"error": "forbidden"}), 403

    conn = get_db_connection()
    rows = conn.execute("""
        SELECT
            g.id,
            g.stage_id,
            g.team_home_id,
            g.team_away_id,
            g.game_datetime,
            g.score_home,
            g.score_away,
            th.name AS home_name,
            ta.name AS away_name,
            (SELECT COUNT(*) FROM predictions WHERE game_id = g.id) AS predictions
        FROM games g
        LEFT JOIN teams th ON g.team_home_id = th.id
        LEFT JOIN teams ta ON g.team_away_id = ta.id
        ORDER BY g.stage_id, g.id
    """).fetchall()
    conn.close()

    from collections import defaultdict
    buckets = defaultdict(list)
    skipped_undefined = 0
    for r in rows:
        # Jogos das eliminatórias podem ter teams NULL (ainda não definidos).
        # Sem times, não dá pra detectar duplicata por par.
        if r["team_home_id"] is None or r["team_away_id"] is None:
            skipped_undefined += 1
            continue
        a = min(r["team_home_id"], r["team_away_id"])
        b = max(r["team_home_id"], r["team_away_id"])
        buckets[(r["stage_id"], a, b)].append(dict(r))

    duplicates = []
    for key, games in buckets.items():
        if len(games) <= 1:
            continue
        # Mesma ordenação que o dedupe vai aplicar: mais palpites primeiro, menor id em empate
        ranked = sorted(games, key=lambda g: (-g["predictions"], g["id"]))
        duplicates.append({
            "stage_id": key[0],
            "team_pair": f"{key[1]}x{key[2]}",
            "would_keep": ranked[0]["id"],
            "would_remove": [g["id"] for g in ranked[1:]],
            "games": ranked,
        })

    return jsonify({
        "total_games": len(rows),
        "skipped_undefined_teams": skipped_undefined,
        "duplicate_pairs": len(duplicates),
        "duplicates": duplicates,
    })


# =========================
# DEDUPE — remove pares duplicados, migra palpites pro jogo mantido
# POST em /admin/games-dedupe (só admin)
# =========================
@app.route("/admin/games-dedupe", methods=["POST"])
def admin_games_dedupe():
    if session.get("user_id") != 1:
        return jsonify({"error": "forbidden"}), 403

    conn = get_db_connection()
    rows = conn.execute("""
        SELECT
            g.id,
            g.stage_id,
            g.team_home_id,
            g.team_away_id,
            (SELECT COUNT(*) FROM predictions WHERE game_id = g.id) AS predictions
        FROM games g
    """).fetchall()

    from collections import defaultdict
    buckets = defaultdict(list)
    for r in rows:
        if r["team_home_id"] is None or r["team_away_id"] is None:
            continue
        a = min(r["team_home_id"], r["team_away_id"])
        b = max(r["team_home_id"], r["team_away_id"])
        buckets[(r["stage_id"], a, b)].append(dict(r))

    report = []
    for key, games in buckets.items():
        if len(games) <= 1:
            continue
        ranked = sorted(games, key=lambda g: (-g["predictions"], g["id"]))
        keeper = ranked[0]["id"]
        losers = [g["id"] for g in ranked[1:]]

        moved_total = 0
        dropped_total = 0
        for loser in losers:
            # Migra palpites do loser pro keeper, exceto quando user já tem palpite no keeper
            cur = conn.execute("""
                UPDATE predictions
                SET game_id = ?
                WHERE game_id = ?
                  AND user_id NOT IN (SELECT user_id FROM predictions WHERE game_id = ?)
            """, (keeper, loser, keeper))
            moved_total += cur.rowcount

            # Apaga os palpites duplicados restantes (user já tinha no keeper)
            cur = conn.execute("DELETE FROM predictions WHERE game_id = ?", (loser,))
            dropped_total += cur.rowcount

            # Remove o jogo duplicado
            conn.execute("DELETE FROM games WHERE id = ?", (loser,))

        report.append({
            "stage_id": key[0],
            "team_pair": f"{key[1]}x{key[2]}",
            "kept": keeper,
            "removed": losers,
            "predictions_moved": moved_total,
            "predictions_dropped": dropped_total,
        })

    conn.commit()
    conn.close()
    return jsonify({"action": "applied", "pairs_dedup": len(report), "report": report})


# =========================
# AUDIT/FIX — país incoerente com a unidade Eureka
# GET  /admin/users-country-audit  → lista inconsistências (dry-run)
# POST /admin/users-country-fix    → corrige country_code pela unidade
# =========================
@app.route("/admin/users-country-audit")
def admin_users_country_audit():
    if session.get("user_id") != 1:
        return jsonify({"error": "forbidden"}), 403

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, name, country_code, eureka_unit FROM users"
    ).fetchall()
    conn.close()

    mismatches = []
    for r in rows:
        expected = country_from_unit(r["eureka_unit"])
        if expected is not None and r["country_code"] != expected:
            mismatches.append({
                "id": r["id"],
                "name": r["name"],
                "eureka_unit": r["eureka_unit"],
                "country_code": r["country_code"],
                "should_be": expected,
            })

    return jsonify({
        "total_users": len(rows),
        "mismatches": len(mismatches),
        "details": mismatches,
    })


@app.route("/admin/users-country-fix", methods=["POST"])
def admin_users_country_fix():
    if session.get("user_id") != 1:
        return jsonify({"error": "forbidden"}), 403

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, name, country_code, eureka_unit FROM users"
    ).fetchall()

    fixed = []
    for r in rows:
        expected = country_from_unit(r["eureka_unit"])
        if expected is not None and r["country_code"] != expected:
            conn.execute(
                "UPDATE users SET country_code = ? WHERE id = ?",
                (expected, r["id"]),
            )
            fixed.append({
                "id": r["id"],
                "name": r["name"],
                "eureka_unit": r["eureka_unit"],
                "from": r["country_code"],
                "to": expected,
            })

    conn.commit()
    conn.close()
    return jsonify({"action": "applied", "fixed": len(fixed), "report": fixed})


# =========================
# AUDIT/FIX — ordem dos times (mando) divergente da API
# GET  /admin/orientation-audit  → lista jogos com mandante invertido (dry-run)
# POST /admin/orientation-fix    → troca times + palpites + placar juntos
#
# Para cada jogo onde a base tem (home,away) na ordem oposta à da API:
#   - troca team_home_id <-> team_away_id
#   - troca predicted_home_score <-> predicted_away_score (todos os palpites)
#   - troca score_home <-> score_away
# Assim o mando passa a refletir o jogo oficial SEM mudar o sentido dos
# palpites nem a pontuação.
# =========================
def _orientation_mismatches(conn):
    """Retorna lista de jogos cuja ordem (home/away) está oposta à da API."""
    fixtures = fetch_world_cup_fixtures()
    mismatches = []
    for item in fixtures:
        teams = item.get("teams", {})
        home_name = teams.get("home", {}).get("name")
        away_name = teams.get("away", {}).get("name")
        fixture_id = item.get("fixture", {}).get("id")
        if not fixture_id or not home_name or not away_name:
            continue

        row = conn.execute(
            "SELECT id FROM games WHERE api_game_id = ?", (str(fixture_id),)
        ).fetchone()
        if row:
            gid = row["id"]
        else:
            nm, _ = find_db_game_by_team_names(conn, home_name, away_name)
            gid = nm["id"] if nm else None
        if not gid:
            continue

        g = conn.execute("""
            SELECT g.score_home, g.score_away,
                   th.name AS home_name, ta.name AS away_name
            FROM games g
            LEFT JOIN teams th ON g.team_home_id = th.id
            LEFT JOIN teams ta ON g.team_away_id = ta.id
            WHERE g.id = ?
        """, (gid,)).fetchone()
        if not g:
            continue

        dh = normalize_team_name(g["home_name"])
        da = normalize_team_name(g["away_name"])
        ah = normalize_team_name(home_name)
        aa = normalize_team_name(away_name)
        # só consideramos mando invertido quando os nomes resolvem na ordem oposta
        if dh == aa and da == ah and dh != da:
            preds = conn.execute(
                "SELECT COUNT(*) AS n FROM predictions WHERE game_id = ?", (gid,)
            ).fetchone()["n"]
            mismatches.append({
                "game_id": gid,
                "db_order": f"{g['home_name']} x {g['away_name']}",
                "api_order": f"{home_name} x {away_name}",
                "predictions": preds,
                "has_score": g["score_home"] is not None,
            })
    return mismatches


@app.route("/admin/orientation-audit")
def admin_orientation_audit():
    if session.get("user_id") != 1:
        return jsonify({"error": "forbidden"}), 403
    try:
        conn = get_db_connection()
        mismatches = _orientation_mismatches(conn)
        conn.close()
    except Exception as e:
        return jsonify({"error": f"falha: {e}"}), 502
    return jsonify({"mismatches": len(mismatches), "details": mismatches})


@app.route("/admin/orientation-fix", methods=["POST"])
def admin_orientation_fix():
    if session.get("user_id") != 1:
        return jsonify({"error": "forbidden"}), 403
    try:
        conn = get_db_connection()
        mismatches = _orientation_mismatches(conn)
        for m in mismatches:
            gid = m["game_id"]
            # troca os times do jogo
            conn.execute("""
                UPDATE games
                SET team_home_id = team_away_id,
                    team_away_id = team_home_id,
                    score_home = score_away,
                    score_away = score_home
                WHERE id = ?
            """, (gid,))
            # troca os palpites (mantém o sentido pro usuário)
            conn.execute("""
                UPDATE predictions
                SET predicted_home_score = predicted_away_score,
                    predicted_away_score = predicted_home_score
                WHERE game_id = ?
            """, (gid,))
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"error": f"falha: {e}"}), 502
    return jsonify({"action": "applied", "fixed": len(mismatches), "report": mismatches})


# =========================
# AUDIT — placares invertidos (dry-run, não altera nada)
# GET /admin/scores-audit
# Compara o placar guardado na base com a orientação correta (calculada
# pelos nomes vs API) e lista os jogos onde divergem.
# =========================
@app.route("/admin/scores-audit")
def admin_scores_audit():
    if session.get("user_id") != 1:
        return jsonify({"error": "forbidden"}), 403

    try:
        fixtures = fetch_world_cup_fixtures()
    except Exception as e:
        return jsonify({"error": f"falha ao buscar fixtures: {e}"}), 502

    conn = get_db_connection()
    FINISHED_STATUSES = {"FT", "AET", "PEN"}
    inverted = []
    finished_checked = 0

    for item in fixtures:
        fixture = item.get("fixture", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})

        if fixture.get("status", {}).get("short") not in FINISHED_STATUSES:
            continue

        home_name = teams.get("home", {}).get("name")
        away_name = teams.get("away", {}).get("name")
        fixture_id = fixture.get("id")
        if not fixture_id or not home_name or not away_name:
            continue

        api_game_id = str(fixture_id)

        # localizar o jogo na base (mesma lógica do sync)
        row = conn.execute(
            "SELECT id FROM games WHERE api_game_id = ?", (api_game_id,)
        ).fetchone()
        if row:
            gid = row["id"]
        else:
            nm, _ = find_db_game_by_team_names(conn, home_name, away_name)
            gid = nm["id"] if nm else None
        if not gid:
            continue

        g = conn.execute("""
            SELECT g.score_home, g.score_away,
                   th.name AS home_name, ta.name AS away_name
            FROM games g
            LEFT JOIN teams th ON g.team_home_id = th.id
            LEFT JOIN teams ta ON g.team_away_id = ta.id
            WHERE g.id = ?
        """, (gid,)).fetchone()
        if not g:
            continue

        finished_checked += 1

        # orientação correta pelos nomes
        dh = normalize_team_name(g["home_name"])
        da = normalize_team_name(g["away_name"])
        ah = normalize_team_name(home_name)
        aa = normalize_team_name(away_name)
        if dh == aa and da == ah:
            swapped = True
        elif dh == ah and da == aa:
            swapped = False
        else:
            continue  # nomes não resolvem — não dá pra afirmar orientação

        sh = goals.get("home")
        sa = goals.get("away")
        correct_home = sa if swapped else sh
        correct_away = sh if swapped else sa

        cur_home = g["score_home"]
        cur_away = g["score_away"]

        # só reporta quando já há placar gravado e ele diverge do correto
        if cur_home is not None and (cur_home, cur_away) != (correct_home, correct_away):
            inverted.append({
                "game_id": gid,
                "home": g["home_name"],
                "away": g["away_name"],
                "stored": f"{cur_home} x {cur_away}",
                "correct": f"{correct_home} x {correct_away}",
                "pure_swap": (cur_home == correct_away and cur_away == correct_home),
            })

    conn.close()
    return jsonify({
        "finished_checked": finished_checked,
        "inverted": len(inverted),
        "details": inverted,
    })


# =========================
# REGRAS DE PONTUAÇÃO
# =========================
def calculate_points(real_home, real_away, pred_home, pred_away, penalty_winner_id=None, predicted_penalty_winner_id=None):
    if pred_home is None or pred_away is None:
        return 0

    real_home = int(real_home)
    real_away = int(real_away)
    pred_home = int(pred_home)
    pred_away = int(pred_away)

    # Placar exato
    if real_home == pred_home and real_away == pred_away:
        # Jogo com pênaltis — empate acertado
        if penalty_winner_id is not None:
            if predicted_penalty_winner_id == penalty_winner_id:
                return 10  # empate acertado + vencedor pênaltis acertado
            else:
                return 7   # empate acertado + vencedor pênaltis errado (ou não escolhido)
        return 10  # placar exato normal

    # Resultado real
    if real_home > real_away:
        real_result = "home"
    elif real_home < real_away:
        real_result = "away"
    else:
        real_result = "draw"

    # Resultado do palpite
    if pred_home > pred_away:
        pred_result = "home"
    elif pred_home < pred_away:
        pred_result = "away"
    else:
        pred_result = "draw"

    acertou_vencedor = real_result == pred_result
    acertou_um_lado = (real_home == pred_home or real_away == pred_away)

    if acertou_vencedor and acertou_um_lado:
        return 7
    if acertou_vencedor:
        return 5
    if acertou_um_lado:
        return 2

    return 0


# =========================
# SYNC INTELIGENTE
# Só corre nos dias com jogos, a cada 15 min
# =========================
def has_games_today():
    """Sincroniza sempre durante a Copa 2026, caso contrário só se houver jogos hoje."""
    from datetime import date
    today = datetime.utcnow().date()
    copa_start = date(2026, 6, 11)
    copa_end = date(2026, 7, 19)

    if copa_start <= today <= copa_end:
        return True

    conn = get_db_connection()
    row = conn.execute("""
        SELECT COUNT(*) as total
        FROM games
        WHERE game_datetime LIKE ?
    """, (today.isoformat() + "%",)).fetchone()
    conn.close()
    return row["total"] > 0


def get_alert_email():
    """Email que recebe alertas operacionais (jogos não sincronizados).

    Ordem: ALERT_EMAIL > ADMIN_BOOTSTRAP_EMAIL > email do admin (user 1).
    """
    email = os.environ.get("ALERT_EMAIL") or os.environ.get("ADMIN_BOOTSTRAP_EMAIL")
    if email:
        return email
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT email FROM users WHERE id = 1").fetchone()
        conn.close()
        return row["email"] if row else None
    except Exception:
        return None


# Assinatura do último alerta enviado — evita reenviar o mesmo a cada 15 min.
_last_skip_alert = {"signature": None}


def alert_skipped_games(skipped_details):
    """Avisa o admin por email quando jogos não casaram no sync.

    Só envia quando o conjunto de jogos pulados MUDA, pra não gerar
    um email a cada execução do scheduler (15 em 15 min).
    """
    signature = ",".join(sorted(str(d.get("fixture_id")) for d in skipped_details))
    if signature == _last_skip_alert["signature"]:
        return  # mesma lista já avisada
    _last_skip_alert["signature"] = signature

    to = get_alert_email()
    if not to:
        print("[sync] ALERT_EMAIL não resolvido — alerta não enviado", flush=True)
        return

    linhas = "".join(
        f"<li><b>{d.get('home_name')}</b> x <b>{d.get('away_name')}</b> "
        f"— motivo: {d.get('reason')} "
        f"(traduzido: {d.get('home_name_pt')} / {d.get('away_name_pt')})</li>"
        for d in skipped_details
    )
    html_body = (
        f"<p>{len(skipped_details)} jogo(s) não foram sincronizados da API "
        f"(nome não casou com a base). O resultado não será gravado até resolver:</p>"
        f"<ul>{linhas}</ul>"
        f"<p>Em geral é um alias de nome faltando no <code>TEAM_NAME_MAP</code>.</p>"
    )
    subject = f"[Arena Eureka] {len(skipped_details)} jogo(s) não sincronizados"
    try:
        send_email(to, subject, html_body)
        print(f"[sync] alerta de {len(skipped_details)} jogos pulados enviado para {to}", flush=True)
    except Exception as e:
        print(f"[sync] falha ao enviar alerta de jogos pulados: {e}", flush=True)


def smart_sync():
    """Só sincroniza se houver jogos hoje ou durante a Copa 2026."""
    if not has_games_today():
        print(f"[sync] Sem jogos hoje ({datetime.utcnow().date()}) — sync ignorado")
        return

    print(f"[sync] Jogos hoje — a sincronizar às {datetime.now().strftime('%H:%M')}")
    try:
        result = sync_games_from_api()
        print(f"[sync] OK — {result['updated_games']} jogos actualizados, {result['skipped_games']} ignorados")
        skipped = result.get("skipped_details") or []
        if skipped:
            alert_skipped_games(skipped)
    except Exception as e:
        print(f"[sync] ERRO — {e}")


# Scheduler: corre a cada 15 minutos
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    func=smart_sync,
    trigger="interval",
    minutes=15,
    id="smart_sync_job",
    replace_existing=True
)
scheduler.start()
import atexit
atexit.register(lambda: scheduler.shutdown(wait=False))

# =========================
# DADOS DO RANKING
# =========================
def get_ranking_data(view=None, filter_value=None):
    conn = get_db_connection()

    # Filtro por país ou unidade
    where_clause = ""
    params = []
    if view == "country" and filter_value:
        where_clause = "WHERE u.country_code = ?"
        params.append(filter_value)
    elif view == "unit" and filter_value:
        where_clause = "WHERE u.eureka_unit = ?"
        params.append(filter_value)

    # Uma única query que busca todos os palpites de todos os utilizadores
    # de uma só vez, em vez de fazer uma query por utilizador.
    #
    # O que o SQL faz:
    # - JOIN entre users, predictions e games
    # - Só considera jogos com resultado (score_home IS NOT NULL)
    # - Agrupa por utilizador (GROUP BY u.id)
    # - Dentro de cada grupo, usa CASE WHEN para calcular pontos
    #   directamente no SQL, sem precisar de Python para isso
    #
    # CASE WHEN em SQL é equivalente a um if/elif/else em Python.
    # SUM() soma todos os valores de uma coluna dentro do grupo.
    # COUNT() com condição conta só as linhas onde a condição é verdade.
    rows = conn.execute(f"""
        SELECT
            u.id,
            u.name,
            u.country_code,
            u.eureka_unit,

            -- Pontos totais: soma os pontos de cada palpite
            COALESCE(SUM(
                CASE
                    -- Placar exato: 10 pontos
                    WHEN p.predicted_home_score = g.score_home
                     AND p.predicted_away_score = g.score_away
                    THEN 10

                    -- Acertou vencedor + um lado: 7 pontos
                    WHEN (
                        (g.score_home > g.score_away AND p.predicted_home_score > p.predicted_away_score) OR
                        (g.score_home < g.score_away AND p.predicted_home_score < p.predicted_away_score) OR
                        (g.score_home = g.score_away AND p.predicted_home_score = p.predicted_away_score)
                    ) AND (
                        p.predicted_home_score = g.score_home OR
                        p.predicted_away_score = g.score_away
                    )
                    THEN 7

                    -- Acertou só vencedor: 5 pontos
                    WHEN (
                        (g.score_home > g.score_away AND p.predicted_home_score > p.predicted_away_score) OR
                        (g.score_home < g.score_away AND p.predicted_home_score < p.predicted_away_score) OR
                        (g.score_home = g.score_away AND p.predicted_home_score = p.predicted_away_score)
                    )
                    THEN 5

                    -- Acertou só um lado: 2 pontos
                    WHEN p.predicted_home_score = g.score_home
                      OR p.predicted_away_score = g.score_away
                    THEN 2

                    -- Nenhum critério: 0 pontos
                    ELSE 0
                END
            ), 0) AS total_points,

            -- Conta quantas vezes acertou o placar exato
            COUNT(CASE
                WHEN p.predicted_home_score = g.score_home
                 AND p.predicted_away_score = g.score_away
                THEN 1
            END) AS exact_hits,

            -- Conta quantas vezes acertou o vencedor (10, 7 ou 5 pts)
            COUNT(CASE
                WHEN (
                    (g.score_home > g.score_away AND p.predicted_home_score > p.predicted_away_score) OR
                    (g.score_home < g.score_away AND p.predicted_home_score < p.predicted_away_score) OR
                    (g.score_home = g.score_away AND p.predicted_home_score = p.predicted_away_score)
                )
                THEN 1
            END) AS winner_hits

        FROM users u
        LEFT JOIN predictions p ON u.id = p.user_id
        LEFT JOIN games g ON p.game_id = g.id
            AND g.score_home IS NOT NULL
            AND g.score_away IS NOT NULL
        {where_clause}
        GROUP BY u.id
        ORDER BY total_points DESC, exact_hits DESC, winner_hits DESC
    """, params).fetchall()

    # Converter para lista de dicionários e adicionar posição
    ranking_data = []
    for index, row in enumerate(rows, start=1):
        ranking_data.append({
            "id": row["id"],
            "name": row["name"],
            "country_code": row["country_code"],
            "eureka_unit": row["eureka_unit"],
            "points": row["total_points"],
            "exact_hits": row["exact_hits"],
            "winner_hits": row["winner_hits"],
            "position": index
        })

    conn.close()
    return ranking_data


# =========================
# DECORATOR DE LOGIN
# =========================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function




# =========================
# CONTEXT PROCESSOR
# Torna 'current_lang' disponível em todos os templates
# =========================
@app.context_processor
def inject_language():
    return dict(current_lang=session.get('lang', 'pt'))

# =========================
# MUDAR IDIOMA
# =========================
@app.route('/set-language/<lang>')
def set_language(lang):
    """Guarda o idioma na sessão e volta à página anterior."""
    if lang in app.config['LANGUAGES']:
        session.permanent = True
        session['lang'] = lang
    # Volta à página de onde veio, ou para home
    return redirect(request.referrer or url_for('home'))



# =========================
# FANZONE — arquivo, acessível em /fanzone
# =========================
@app.route("/fanzone")
def landing():
    ranking = get_ranking_data()[:5]
    return render_template("landing.html", ranking=ranking)


# =========================
# HOME PÚBLICA DO BOLÃO — página principal
# =========================
@app.route("/")
@app.route("/home")
def home():
    # Se já está logado vai directo para o perfil
    if "user_id" in session:
        return redirect(url_for("me"))

    conn = get_db_connection()

    rows = conn.execute("""
        SELECT
            teams.name AS team_name,
            teams.flag_code,
            groups.name AS group_name
        FROM teams
        JOIN groups ON teams.group_id = groups.id
        ORDER BY groups.id, teams.name
    """).fetchall()

    groups = {}
    for row in rows:
        group = row["group_name"]
        if group not in groups:
            groups[group] = []
        groups[group].append({
            "name": row["team_name"],
            "flag_code": row["flag_code"]
        })

    conn.close()

    ranking = get_ranking_data()[:5]

    return render_template(
        "home.html",
        groups=groups,
        ranking=ranking
    )


# =========================
# RANKING GERAL
# Público: top 5
# Logado: ranking completo
# =========================
@app.route("/ranking")
def ranking():
    view = request.args.get("view", "general")
    filter_value = request.args.get("filter")

# segurança para evitar valores inesperados

    allowed_views = ["general", "country", "unit"]
    if view not in allowed_views:
        view = "general"

    if view == "general":
        filter_value = None

    ranking_data = get_ranking_data(view=view, filter_value=filter_value)

    is_logged = "user_id" in session
    current_user_id = session.get("user_id")

    if not is_logged:
        ranking_data = ranking_data[:5]


    country_filters = [
        ("PT", "Portugal"),
        ("BR", "Brasil"),
    ]

    unit_filters = [
        ("lisboa", "Lisboa"),
        ("campinas", "Campinas"),
        ("sao_paulo", "São Paulo"),
    ]

    return render_template(
        "ranking.html",
        ranking=ranking_data,
        is_logged=is_logged,
        current_user_id=current_user_id,
        view=view,
        filter_value=filter_value,
        country_filters=country_filters,
        unit_filters=unit_filters
    )


# =========================
# LOGIN
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    login_error = None
    reset = request.args.get("reset")

    if request.method == "POST":
        ip = request.remote_addr

        # Verificar se o IP está bloqueado
        if is_rate_limited(ip):
            login_error = _("Too many failed attempts. Please try again in %(minutes)d minutes.", minutes=LOCKOUT_MINUTES)
            return render_template("login.html", login_error=login_error, reset=reset)

        email = request.form["email"]
        password = request.form["password"]

        conn = get_db_connection()
        user = conn.execute(
            "SELECT * FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        conn.close()

        if user and user["password_hash"] and check_password_hash(user["password_hash"], password):
            clear_attempts(ip)

            # Verificar se o email foi confirmado
            if not user["email_verified"]:
                # Gerar novo token e mostrar no terminal
                token = serializer.dumps(user["email"], salt="email-verification")
                send_verification_email(user["email"], token)
                login_error = _("Please verify your email before signing in. A new verification link has been sent.")
                return render_template("login.html", login_error=login_error, reset=reset)

            session["user_id"] = user["id"]
            return redirect(url_for("me"))

        # Registar tentativa falhada
        register_failed_attempt(ip)
        remaining = MAX_ATTEMPTS - len(login_attempts[ip])

        if remaining <= 0:
            login_error = _("Too many failed attempts. Please try again in %(minutes)d minutes.", minutes=LOCKOUT_MINUTES)
        elif remaining == 1:
            login_error = _("Invalid login. Last attempt before temporary block.")
        else:
            login_error = _("Invalid login. %(remaining)d attempts remaining.", remaining=remaining)

    verified = request.args.get("verified")
    return render_template(
        "login.html",
        login_error=login_error,
        reset=reset,
        verified=verified
    )


# =========================
# CADASTRO
# =========================
@app.route("/register", methods=["GET", "POST"])
def register():
    register_error = None

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]
        eureka_unit = request.form["eureka_unit"]

        # País é derivado da unidade, não escolhido separadamente — assim
        # bandeira e cidade no ranking nunca ficam incoerentes.
        country_code = country_from_unit(eureka_unit)
        if country_code is None:
            register_error = _("Please select a valid Eureka unit.")
            return render_template("register.html", register_error=register_error)

        if not request.form.get("rules_consent"):
            register_error = _("You must accept the Arena Eureka Rules to create an account.")
            return render_template("register.html", register_error=register_error)

        password_hash = generate_password_hash(password)

        conn = get_db_connection()

        existing_user = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if existing_user:
            conn.close()
            register_error = "Este email já está cadastrado"
            return render_template("register.html", register_error=register_error)

        conn.execute("""
            INSERT INTO users (
                name,
                email,
                password_hash,
                created_at,
                country_code,
                eureka_unit
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            name,
            email,
            password_hash,
            datetime.now(),
            country_code,
            eureka_unit
        ))

        conn.commit()

        # Login automático após registo
        new_user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        session["user_id"] = new_user["id"]

        # Gerar token de verificação e mostrar no terminal
        # Em produção: enviar por email
        token = serializer.dumps(email, salt="email-verification")
        send_verification_email(email, token)

        return redirect(url_for("register_success", name=name))

    return render_template("register.html", register_error=register_error)


# =========================
# REGISTO CONCLUÍDO
# =========================
@app.route("/register/success")
def register_success():
    name = request.args.get("name", "")
    return render_template("register_success.html", name=name)



# =========================
# DASHBOARD DO USUÁRIO
# =========================
@app.route("/me")
@login_required
def me():
    user_id = session["user_id"]
    conn = get_db_connection()

    user = conn.execute("""
        SELECT id, name, email, email_verified
        FROM users
        WHERE id = ?
    """, (user_id,)).fetchone()

    # Jogos já finalizados + palpites do usuário
    rows = conn.execute("""
        SELECT
            g.score_home,
            g.score_away,
            p.predicted_home_score,
            p.predicted_away_score
        FROM games g
        JOIN predictions p
            ON g.id = p.game_id
        WHERE p.user_id = ?
          AND g.score_home IS NOT NULL
          AND g.score_away IS NOT NULL
    """, (user_id,)).fetchall()

    total_points = 0
    exact_hits = 0
    winner_plus_side_hits = 0
    winner_only_hits = 0
    one_side_hits = 0
    scored_games = 0

    for row in rows:
        points = calculate_points(
            row["score_home"],
            row["score_away"],
            row["predicted_home_score"],
            row["predicted_away_score"]
        )

        total_points += points
        scored_games += 1

        if points == 10:
            exact_hits += 1
        elif points == 7:
            winner_plus_side_hits += 1
        elif points == 5:
            winner_only_hits += 1
        elif points == 2:
            one_side_hits += 1

    # Posição no ranking
    ranking_data = get_ranking_data()
    position = 0

    for user_rank in ranking_data:
        if user_rank["id"] == user_id:
            position = user_rank["position"]
            break

    # Aproveitamento baseado em jogos encerrados
    aproveitamento = 0
    if scored_games > 0:
        aproveitamento = round((total_points / (scored_games * 10)) * 100)

    # Jogos de hoje
    today = datetime.now().date()
    rows_today = conn.execute("""
        SELECT
            g.game_datetime,
            th.name AS home_name,
            ta.name AS away_name
        FROM games g
        LEFT JOIN teams th ON g.team_home_id = th.id
        LEFT JOIN teams ta ON g.team_away_id = ta.id
    """).fetchall()

    today_games = []
    for row in rows_today:
        game_datetime = datetime.fromisoformat(row["game_datetime"].strip().replace('+00:00', '').replace('Z', ''))
        game_date = game_datetime.date()
        if game_date == today:
            today_games.append(row)

    # Próximos jogos ainda sem palpite
    now = datetime.utcnow()
    rows_next = conn.execute("""
        SELECT
            g.id,
            g.game_datetime,
            th.name AS home_name,
            ta.name AS away_name,
            p.id AS prediction_id
        FROM games g
        LEFT JOIN teams th ON g.team_home_id = th.id
        LEFT JOIN teams ta ON g.team_away_id = ta.id
        LEFT JOIN predictions p
            ON g.id = p.game_id
            AND p.user_id = ?
    """, (user_id,)).fetchall()

    upcoming_games = []
    for row in rows_next:
        game_datetime = datetime.fromisoformat(row["game_datetime"].strip().replace('+00:00', '').replace('Z', ''))
        if game_datetime > now and row["prediction_id"] is None:
            upcoming_games.append(row)
        if len(upcoming_games) == 5:
            break

    top_ranking = ranking_data[:5]

    conn.close()

    # delete_error=1 aparece quando a password de confirmação estava errada
    delete_error = request.args.get("delete_error")
    # resent=1 aparece quando o utilizador pediu reenvio do link
    resent = request.args.get("resent")
    # verified vem do login após confirmar o email
    email_verified = user["email_verified"] if user else 0

    return render_template(
        "me.html",
        user=user,
        total_points=total_points,
        position=position,
        exact_hits=exact_hits,
        winner_plus_side_hits=winner_plus_side_hits,
        winner_only_hits=winner_only_hits,
        one_side_hits=one_side_hits,
        aproveitamento=aproveitamento,
        today_games=today_games,
        upcoming_games=upcoming_games,
        top_ranking=top_ranking,
        delete_error=delete_error,
        email_verified=email_verified,
        resent=resent
    )

# =========================
# PALPITES
# =========================
@app.route("/predict", methods=["GET", "POST"])
@login_required
def predict():
    user_id = session["user_id"]
    conn = get_db_connection()
    saved_game = request.args.get("saved_game")

    # -------------------
    # POST: salvar palpite
    # -------------------
    if request.method == "POST":
        game_id = request.form["game_id"]
        home_score = request.form["home_score"]
        away_score = request.form["away_score"]

        game = conn.execute("""
            SELECT game_datetime
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

        raw_datetime = game["game_datetime"].strip()
        game_datetime = datetime.fromisoformat(raw_datetime.replace('+00:00', '').replace('Z', ''))

        now = datetime.utcnow()

        if now >= game_datetime:
            conn.close()
            # 403 (não 200) pra o front detectar a rejeição e travar o card.
            return jsonify({
                "error": "locked",
                "message": "Esse jogo já começou. Palpite bloqueado."
            }), 403

        predicted_penalty_winner_id = request.form.get("predicted_penalty_winner_id") or None

        conn.execute("""
         INSERT OR REPLACE INTO predictions
         (user_id, game_id, predicted_home_score, predicted_away_score, predicted_penalty_winner_id, created_at)
          VALUES (?, ?, ?, ?, ?, ?)
        """, (
        user_id,
        game_id,
        home_score,
        away_score,
        predicted_penalty_winner_id,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
))

        conn.commit()
        conn.close()

        return redirect(url_for("predict", saved_game=game_id))

    # -------------------
    # GET: listar jogos e palpites
    # -------------------
    user_row = conn.execute("SELECT country_code FROM users WHERE id = ?", (user_id,)).fetchone()
    display_tz = user_timezone(user_row["country_code"] if user_row else None)

    rows = conn.execute("""
        SELECT
            g.id,
            g.game_datetime,
            g.score_home,
            g.score_away,
            g.stage_id,
            s.name AS stage_name,

            th.name AS home_name,
            th.flag_code AS home_flag,
            gh.name AS home_group_name,

            ta.name AS away_name,
            ta.flag_code AS away_flag,
            ga.name AS away_group_name,

            p.predicted_home_score,
            p.predicted_away_score,
            g.penalty_winner_id,
            p.predicted_penalty_winner_id

        FROM games g
        JOIN stages s ON g.stage_id = s.id

        LEFT JOIN teams th ON g.team_home_id = th.id
        LEFT JOIN groups gh ON th.group_id = gh.id

        LEFT JOIN teams ta ON g.team_away_id = ta.id
        LEFT JOIN groups ga ON ta.group_id = ga.id

        LEFT JOIN predictions p
            ON g.id = p.game_id
            AND p.user_id = ?

        ORDER BY g.stage_id, g.game_datetime
    """, (user_id,)).fetchall()

    now = datetime.utcnow()
    stages_map = {}

    total_points = 0
    total_open = 0
    total_games = 0

    for row in rows:
        real_home = row["score_home"]
        real_away = row["score_away"]
        pred_home = row["predicted_home_score"]
        pred_away = row["predicted_away_score"]

        points = 0
        if real_home is not None and real_away is not None:
            points = calculate_points(
                real_home,
                real_away,
                pred_home,
                pred_away
            )

        raw_datetime = row["game_datetime"].strip()
        game_datetime = datetime.fromisoformat(raw_datetime.replace('+00:00', '').replace('Z', ''))
        is_locked = now >= game_datetime
        # epoch em ms (UTC) — o front usa pra travar o card no horário,
        # sem ambiguidade de fuso (game_datetime é sempre UTC).
        kickoff_ms = int(game_datetime.replace(tzinfo=timezone.utc).timestamp() * 1000)

        # Formatar data no fuso preferido do usuário (BR ou PT)
        try:
            dt_str_clean = raw_datetime.replace('T', ' ').replace('+00:00', '').replace('Z', '')
            dt_utc = datetime.fromisoformat(dt_str_clean)
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone(display_tz)
            game_datetime_display = dt_local.strftime("%d/%m · %H:%M")
        except Exception:
            game_datetime_display = raw_datetime[:16]

        game_data = {
            "id": row["id"],
            "home_name": row["home_name"],
            "home_flag": row["home_flag"],
            "home_group_name": row["home_group_name"],
            "away_name": row["away_name"],
            "away_flag": row["away_flag"],
            "away_group_name": row["away_group_name"],
            "game_datetime": row["game_datetime"],
            "game_datetime_display": game_datetime_display,
            "kickoff_ms": kickoff_ms,
            "predicted_home_score": row["predicted_home_score"],
            "predicted_away_score": row["predicted_away_score"],
            "points": points,
            "score_home": real_home,
            "score_away": real_away,
            "is_locked": is_locked,
            "penalty_winner_id": row["penalty_winner_id"] if "penalty_winner_id" in row.keys() else None,
            "predicted_penalty_winner_id": row["predicted_penalty_winner_id"] if "predicted_penalty_winner_id" in row.keys() else None,
            "stage_id": row["stage_id"],
            "home_team_id": row["team_home_id"] if "team_home_id" in row.keys() else None,
            "away_team_id": row["team_away_id"] if "team_away_id" in row.keys() else None,
        }

        stage_id = row["stage_id"]
        stage_name = row["stage_name"]

        if stage_id not in stages_map:
            stages_map[stage_id] = {
                "id": stage_id,
                "name": stage_name,
                "groups": {}
            }

        # Primeira fase separada por grupos
        if stage_id == 1:
            group_name = row["home_group_name"] or row["away_group_name"] or "Sem grupo"
        else:
            group_name = stage_name

        if group_name not in stages_map[stage_id]["groups"]:
            stages_map[stage_id]["groups"][group_name] = []

        stages_map[stage_id]["groups"][group_name].append(game_data)

        total_games += 1
        total_points += points

        if not is_locked:
            total_open += 1

    # -------------------
    # Fases fixas do bolão
    # Mesmo sem jogos, a aba aparece
    # -------------------
    default_stages = [
    {"id": 1, "name": "Group Stage"},
    {"id": 2, "name": "Round of 32"},
    {"id": 3, "name": "Round of 16"},
    {"id": 4, "name": "Quarter-finals"},
    {"id": 5, "name": "Semi-finals"},
    {"id": 6, "name": "Third Place"},
    {"id": 7, "name": "Final"},
]

    stages = []

    for default_stage in default_stages:
        stage_id = default_stage["id"]
        stage_name = default_stage["name"]

        if stage_id in stages_map:
            stages.append(stages_map[stage_id])
        else:
            stages.append({
                "id": stage_id,
                "name": stage_name,
                "groups": {}
            })

    # -------------------
    # Ordenar grupos da primeira fase
    # -------------------
    group_order = [
        "Grupo A", "Grupo B", "Grupo C", "Grupo D",
        "Grupo E", "Grupo F", "Grupo G", "Grupo H",
        "Grupo I", "Grupo J", "Grupo K", "Grupo L"
    ]

    for stage in stages:
        if stage["groups"]:
            ordered_groups = {}

            for group_name in group_order:
                if group_name in stage["groups"]:
                    ordered_groups[group_name] = stage["groups"][group_name]

            for group_name, games_list in stage["groups"].items():
                if group_name not in ordered_groups:
                    ordered_groups[group_name] = games_list

            stage["groups"] = ordered_groups

    active_stage = stages[0]["id"] if stages else None

    teams = conn.execute("SELECT DISTINCT name FROM teams ORDER BY name").fetchall()
    teams_list = [t["name"] for t in teams]
    conn.close()

    return render_template(
        "predict.html",
        stages=stages,
        saved_game=saved_game,
        total_games=total_games,
        total_points=total_points,
        total_open=total_open,
        active_stage=active_stage,
        teams_list=teams_list
    )

# =========================
# PÁGINA DE JOGOS
# =========================
@app.route("/games")
def games():
    conn = get_db_connection()

    rows = conn.execute("""
        SELECT
            g.game_datetime,
            s.name AS stage_name,
            th.name AS home_name,
            ta.name AS away_name,
            g.score_home,
            g.score_away
        FROM games g
        JOIN stages s ON g.stage_id = s.id
        LEFT JOIN teams th ON g.team_home_id = th.id
        LEFT JOIN teams ta ON g.team_away_id = ta.id
        ORDER BY g.game_datetime
    """).fetchall()

    conn.close()

    return render_template("games.html", games=rows)



# =========================
# BETTALKS
# =========================
@app.route("/bettalks", methods=["GET", "POST"])
@login_required
def bettalks():
    user_id = session["user_id"]
    conn = get_db_connection()
    error = None

    # -------------------
    # CRIAR NOVO POST
    # -------------------
    if request.method == "POST":
        content = request.form["content"].strip()

        if not content:
            error = "Escreva uma mensagem antes de publicar."
        elif len(content) > 500:
            error = "A mensagem pode ter no máximo 500 caracteres."
        else:
            conn.execute("""
                INSERT INTO bettalks_posts (user_id, content, created_at)
                VALUES (?, ?, ?)
            """, (
                user_id,
                content,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))
            conn.commit()
            conn.close()
            return redirect(url_for("bettalks"))

    # -------------------
    # USUÁRIO ATUAL
    # -------------------
    current_user = conn.execute("""
        SELECT id, name, country_code, eureka_unit, is_admin
        FROM users
        WHERE id = ?
    """, (user_id,)).fetchone()

    # -------------------
    # POSTS
    # -------------------
    posts_rows = conn.execute("""
        SELECT
            bp.id,
            bp.content,
            bp.created_at,
            u.id AS user_id,
            u.name,
            u.country_code,
            u.eureka_unit
        FROM bettalks_posts bp
        JOIN users u ON bp.user_id = u.id
        ORDER BY bp.created_at DESC
    """).fetchall()

    # -------------------
    # COMENTÁRIOS
    # -------------------
    comments_rows = conn.execute("""
        SELECT
            bc.id,
            bc.post_id,
            bc.content,
            bc.created_at,
            u.id AS user_id,
            u.name,
            u.country_code,
            u.eureka_unit
        FROM bettalks_comments bc
        JOIN users u ON bc.user_id = u.id
        ORDER BY bc.created_at ASC
    """).fetchall()

    # -------------------
    # LIKES
    # -------------------
    likes_rows = conn.execute("""
        SELECT post_id, user_id
        FROM bettalks_likes
    """).fetchall()

    conn.close()

    # -------------------
    # ORGANIZAR COMENTÁRIOS POR POST
    # -------------------
    comments_by_post = {}
    for comment in comments_rows:
        post_id = comment["post_id"]

        if post_id not in comments_by_post:
            comments_by_post[post_id] = []

        comments_by_post[post_id].append({
            "id": comment["id"],
            "post_id": comment["post_id"],
            "content": comment["content"],
            "created_at": comment["created_at"],
            "user_id": comment["user_id"],
            "name": comment["name"],
            "country_code": comment["country_code"],
            "eureka_unit": comment["eureka_unit"]
        })

    # -------------------
    # ORGANIZAR LIKES POR POST
    # -------------------
    likes_by_post = {}
    for like in likes_rows:
        post_id = like["post_id"]

        if post_id not in likes_by_post:
            likes_by_post[post_id] = set()

        likes_by_post[post_id].add(like["user_id"])

    # -------------------
    # MONTAR POSTS FINAIS
    # -------------------
    posts = []
    for post in posts_rows:
        post_likes = likes_by_post.get(post["id"], set())

        posts.append({
            "id": post["id"],
            "content": post["content"],
            "created_at": post["created_at"],
            "user_id": post["user_id"],
            "name": post["name"],
            "country_code": post["country_code"],
            "eureka_unit": post["eureka_unit"],
            "comments": comments_by_post.get(post["id"], []),
            "likes_count": len(post_likes),
            "liked_by_current_user": user_id in post_likes
        })

    return render_template(
        "bettalks.html",
        posts=posts,
        current_user=current_user,
        error=error
    )

@app.route("/bettalks/delete/<int:post_id>", methods=["POST"])
@login_required
def delete_bettalks_post(post_id):
    user_id = session["user_id"]
    conn = get_db_connection()

    current_user = conn.execute("""
        SELECT id, is_admin
        FROM users
        WHERE id = ?
    """, (user_id,)).fetchone()

    post = conn.execute("""
        SELECT id, user_id
        FROM bettalks_posts
        WHERE id = ?
    """, (post_id,)).fetchone()

    if not post:
        conn.close()
        return "Post não encontrado.", 404

    if post["user_id"] != user_id and current_user["is_admin"] != 1:
        conn.close()
        return "Você não tem permissão para apagar este post.", 403

    # apaga comentários do post primeiro
    conn.execute("""
        DELETE FROM bettalks_comments
        WHERE post_id = ?
    """, (post_id,))

    # apaga likes do post
    conn.execute("""
        DELETE FROM bettalks_likes
        WHERE post_id = ?
    """, (post_id,))

    # apaga o post
    conn.execute("""
        DELETE FROM bettalks_posts
        WHERE id = ?
    """, (post_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("bettalks"))

# =========================
# APAGAR COMENTÁRIO BETTALKS
# =========================
@app.route("/bettalks/comment/delete/<int:comment_id>", methods=["POST"])
@login_required
def delete_bettalks_comment(comment_id):
    user_id = session["user_id"]
    conn = get_db_connection()

    current_user = conn.execute("""
        SELECT id, is_admin
        FROM users
        WHERE id = ?
    """, (user_id,)).fetchone()

    comment = conn.execute("""
        SELECT id, user_id
        FROM bettalks_comments
        WHERE id = ?
    """, (comment_id,)).fetchone()

    if not comment:
        conn.close()
        return "Comentário não encontrado.", 404

    if comment["user_id"] != user_id and current_user["is_admin"] != 1:
        conn.close()
        return "Você não tem permissão para apagar este comentário.", 403

    conn.execute("""
        DELETE FROM bettalks_comments
        WHERE id = ?
    """, (comment_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("bettalks"))

# =========================
# CRIAR COMENTÁRIO BETTALKS
# =========================
@app.route("/bettalks/comment/<int:post_id>", methods=["POST"])
@login_required
def create_bettalks_comment(post_id):
    user_id = session["user_id"]
    content = request.form["content"].strip()

    if not content:
        return redirect(url_for("bettalks"))

    if len(content) > 300:
        return redirect(url_for("bettalks"))

    conn = get_db_connection()

    post = conn.execute("""
        SELECT id
        FROM bettalks_posts
        WHERE id = ?
    """, (post_id,)).fetchone()

    if not post:
        conn.close()
        return "Post não encontrado.", 404

    conn.execute("""
        INSERT INTO bettalks_comments (post_id, user_id, content, created_at)
        VALUES (?, ?, ?, ?)
    """, (
        post_id,
        user_id,
        content,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()
    conn.close()

    return redirect(url_for("bettalks"))

# =========================
# LIKE / UNLIKE BETTALKS
# =========================
@app.route("/bettalks/like/<int:post_id>", methods=["POST"])
@login_required
def toggle_bettalks_like(post_id):
    user_id = session["user_id"]
    conn = get_db_connection()

    post = conn.execute("""
        SELECT id
        FROM bettalks_posts
        WHERE id = ?
    """, (post_id,)).fetchone()

    if not post:
        conn.close()
        return "Post não encontrado.", 404

    existing_like = conn.execute("""
        SELECT id
        FROM bettalks_likes
        WHERE post_id = ? AND user_id = ?
    """, (post_id, user_id)).fetchone()

    if existing_like:
        conn.execute("""
            DELETE FROM bettalks_likes
            WHERE post_id = ? AND user_id = ?
        """, (post_id, user_id))
    else:
        conn.execute("""
            INSERT INTO bettalks_likes (post_id, user_id, created_at)
            VALUES (?, ?, ?)
        """, (
            post_id,
            user_id,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

    conn.commit()
    conn.close()

    return redirect(url_for("bettalks"))

# =========================
# FORGOT PASSWORD
# Solicita recuperação por email
# =========================
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    error = None
    success = None

    if request.method == "POST":
        email = request.form["email"]

        conn = get_db_connection()
        user = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        conn.close()

        # Mesmo se não existir, mostramos mensagem genérica
        # para não expor emails cadastrados
        if user:
            token = serializer.dumps(email, salt="reset-password")
            reset_link = url_for("reset_password", token=token, _external=True)
            html_body = render_template("emails/reset_password.html", reset_link=reset_link)
            subject = _("Reset your password — Arena Eureka")
            try:
                send_email(email, subject, html_body)
            except Exception as e:
                print(f"[mailer] falha ao enviar reset para {email}: {e}", flush=True)

        success = "Se o email existir, enviamos instruções para redefinir a senha."

    return render_template(
        "forgot_password.html",
        error=error,
        success=success
    )


# =========================
# RESET PASSWORD COM TOKEN
# =========================
@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    error = None

    try:
        email = serializer.loads(
            token,
            salt="reset-password",
            max_age=3600  # 1 hora de validade
        )
    except:
        return "Link inválido ou expirado."

    if request.method == "POST":
        password = request.form["password"]
        confirm = request.form["confirm"]

        if password != confirm:
            error = "As senhas não coincidem."
            return render_template("reset_password.html", error=error)

        password_hash = generate_password_hash(password)

        conn = get_db_connection()
        conn.execute("""
            UPDATE users
            SET password_hash = ?
            WHERE email = ?
        """, (password_hash, email))
        conn.commit()
        conn.close()

        return redirect(url_for("login", reset="ok"))

    return render_template("reset_password.html", error=error)

# =========================
# REGRAS BOLAO
# =========================

@app.route("/rules")
def rules():
    is_logged = "user_id" in session
    return render_template("rules.html", is_logged=is_logged)

# =========================
# SYNC
# =========================
@app.route("/sync-games")
@login_required
def sync_games():
    conn_check = get_db_connection()
    current_user = conn_check.execute("SELECT is_admin FROM users WHERE id = ?", (session.get("user_id"),)).fetchone()
    conn_check.close()
    if not current_user or current_user["is_admin"] != 1:
        return {"status": "error", "message": "Acesso negado"}, 403

    try:
        result = sync_games_from_api()
        return {
            "status": "ok",
            "matched_games": result["matched_games"],
            "updated_games": result["updated_games"],
            "skipped_games": result["skipped_games"],
            "skipped_details": result["skipped_details"]
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }, 500




# =========================
# API — RESULTADOS PARA POLLING
# O predict.js chama esta rota a cada 2 min
# para verificar se há resultados novos
# =========================
@app.route("/api/results")
@login_required
def api_results():
    conn = get_db_connection()
    rows = conn.execute("""
        SELECT id, score_home, score_away
        FROM games
        WHERE score_home IS NOT NULL
          AND score_away IS NOT NULL
    """).fetchall()
    conn.close()


    return jsonify({
        str(row["id"]): {
            "score_home": row["score_home"],
            "score_away": row["score_away"]
        }
        for row in rows
    })





# =========================
# APAGAR CONTA
# Exigido pelo RGPD — direito ao esquecimento
# =========================
@app.route("/delete-account", methods=["POST"])
@login_required
def delete_account():
    user_id = session["user_id"]

    # Pedir confirmação com a password actual
    # Evita que alguém apague a conta de outro por acidente ou má intenção
    password = request.form.get("password", "")

    conn = get_db_connection()

    # Buscar o utilizador para verificar a password
    user = conn.execute(
        "SELECT password_hash FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    # Verificar se a password está correcta antes de apagar qualquer coisa
    if not user or not check_password_hash(user["password_hash"], password):
        conn.close()
        # Redirecionar de volta ao perfil com mensagem de erro
        return redirect(url_for("me", delete_error="1"))

    # --- Apagar todos os dados do utilizador ---
    # Ordem importante: primeiro apagar os dados dependentes,
    # depois apagar o utilizador
    # (por causa das FOREIGN KEYS na base de dados)

    # 1. Apagar likes do utilizador nos posts
    conn.execute("DELETE FROM bettalks_likes WHERE user_id = ?", (user_id,))

    # 2. Apagar comentários do utilizador
    conn.execute("DELETE FROM bettalks_comments WHERE user_id = ?", (user_id,))

    # 3. Apagar posts do utilizador (os likes e comentários desses posts
    #    também precisam de ser apagados primeiro)
    posts = conn.execute(
        "SELECT id FROM bettalks_posts WHERE user_id = ?", (user_id,)
    ).fetchall()

    for post in posts:
        conn.execute("DELETE FROM bettalks_likes WHERE post_id = ?", (post["id"],))
        conn.execute("DELETE FROM bettalks_comments WHERE post_id = ?", (post["id"],))

    conn.execute("DELETE FROM bettalks_posts WHERE user_id = ?", (user_id,))

    # 4. Apagar palpites do utilizador
    conn.execute("DELETE FROM predictions WHERE user_id = ?", (user_id,))

    # 5. Finalmente apagar o utilizador
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    conn.commit()
    conn.close()

    # Limpar a sessão — utilizador fica deslogado
    session.clear()

    # Redirecionar para a home com mensagem de confirmação
    return redirect(url_for("home", deleted="1"))


# =========================
# VERIFICAÇÃO DE EMAIL
# =========================

def send_verification_email(email, token):
    verify_link = url_for("verify_email", token=token, _external=True)
    html_body = render_template("emails/verify_email.html", verify_link=verify_link)
    subject = _("Confirm your email — Arena Eureka")
    try:
        send_email(email, subject, html_body)
    except Exception as e:
        print(f"[mailer] falha ao enviar verificação para {email}: {e}", flush=True)


@app.route("/verify-email/<token>")
def verify_email(token):
    try:
        email = serializer.loads(token, salt="email-verification", max_age=86400)
    except Exception:
        return render_template("verify_email_error.html")

    conn = get_db_connection()
    conn.execute("UPDATE users SET email_verified = 1 WHERE email = ?", (email,))
    conn.commit()
    conn.close()
    return redirect(url_for("login", verified="1"))


@app.route("/resend-verification")
@login_required
def resend_verification():
    user_id = session["user_id"]
    conn = get_db_connection()
    user = conn.execute(
        "SELECT email, email_verified FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()

    if user["email_verified"]:
        return redirect(url_for("me"))

    token = serializer.dumps(user["email"], salt="email-verification")
    send_verification_email(user["email"], token)
    return redirect(url_for("me", resent="1"))


# =========================
# POLÍTICA DE PRIVACIDADE
# =========================
@app.route("/privacy")
def privacy():
    is_logged = "user_id" in session
    return render_template("privacy.html", is_logged=is_logged)


# =========================
# ROTA DE ADMIN — RENOMEAR FASE
# =========================
@app.route("/admin/rename-stage/<int:stage_id>/<string:novo_nome>")
def rename_stage(stage_id, novo_nome):
    conn = get_db_connection()
    user = conn.execute("SELECT is_admin FROM users WHERE id = ?", (session.get("user_id"),)).fetchone()
    if not user or user["is_admin"] != 1:
        conn.close()
        return jsonify({"status": "error", "message": "Acesso negado"}), 403

    stage = conn.execute("SELECT id, name FROM stages WHERE id = ?", (stage_id,)).fetchone()
    if not stage:
        conn.close()
        return jsonify({"status": "error", "message": f"Fase {stage_id} não encontrada"}), 404

    nome_antigo = stage["name"]
    conn.execute("UPDATE stages SET name = ? WHERE id = ?", (novo_nome, stage_id))
    conn.commit()
    conn.close()

    return jsonify({
        "status": "ok",
        "message": f"Fase {stage_id} renomeada com sucesso",
        "nome_antigo": nome_antigo,
        "nome_novo": novo_nome
    })

# =========================
# ROTA DE ADMIN — ADICIONAR COLUNAS DE PÊNALTIS
# Corre UMA VEZ — APAGAR depois
# =========================
@app.route("/admin/add-penalty-columns")
def add_penalty_columns():
    conn = get_db_connection()
    user = conn.execute("SELECT is_admin FROM users WHERE id = ?", (session.get("user_id"),)).fetchone()
    if not user or user["is_admin"] != 1:
        conn.close()
        return jsonify({"status": "error", "message": "Acesso negado"}), 403

    results = []
    for sql in [
        "ALTER TABLE games ADD COLUMN penalty_winner_id INTEGER",
        "ALTER TABLE predictions ADD COLUMN predicted_penalty_winner_id INTEGER",
    ]:
        try:
            conn.execute(sql)
            results.append(f"✓ {sql}")
        except Exception as e:
            results.append(f"✗ {sql} — {e}")

    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "results": results})

# =========================
# LOGOUT
# =========================
@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# =========================
# BOOTSTRAP DE ADMIN (uso único)
# Remover este bloco depois de criar o primeiro admin em produção.
# Requer env vars ADMIN_BOOTSTRAP_TOKEN e ADMIN_BOOTSTRAP_EMAIL.
# =========================
@app.route("/_bootstrap-admin")
def bootstrap_admin():
    import hmac
    expected_token = os.environ.get("ADMIN_BOOTSTRAP_TOKEN")
    admin_email = os.environ.get("ADMIN_BOOTSTRAP_EMAIL")
    admin_password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD")

    if not expected_token or not admin_email or not admin_password:
        return ("Not Found", 404)

    provided = request.args.get("token", "")
    if not hmac.compare_digest(provided, expected_token):
        return ("Not Found", 404)

    conn = get_db_connection()
    try:
        existing = conn.execute(
            "SELECT id, is_admin FROM users WHERE email = ?",
            (admin_email,)
        ).fetchone()

        now = datetime.now()
        if existing:
            conn.execute(
                "UPDATE users SET is_admin = 1, email_verified = 1 WHERE id = ?",
                (existing["id"],)
            )
            action = "promoted"
            user_id = existing["id"]
        else:
            pw_hash = generate_password_hash(admin_password)
            admin_name = os.environ.get("ADMIN_BOOTSTRAP_NAME") or admin_email.split("@")[0].replace(".", " ").title()
            cur = conn.execute("""
                INSERT INTO users (
                    name, email, password_hash, created_at,
                    country_code, eureka_unit, is_admin,
                    privacy_consent_at, email_verified
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, 1)
            """, (
                admin_name, admin_email, pw_hash, now,
                "BR", "paulista", now
            ))
            action = "created"
            user_id = cur.lastrowid

        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "action": action, "user_id": user_id, "email": admin_email})


# =========================
# RODA A APLICAÇÃO
# =========================
if __name__ == "__main__":
    app.run(debug=False, port=5001)
