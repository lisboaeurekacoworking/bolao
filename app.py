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


# achar jogo por nome do time
def find_db_game_by_team_names(conn, home_name, away_name):
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
            return row

    return None

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

        score_home = goals.get("home")
        score_away = goals.get("away")

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

        # tenta achar pelo api_game_id
        db_game = conn.execute("""
            SELECT id, api_game_id
            FROM games
            WHERE api_game_id = ?
        """, (api_game_id,)).fetchone()

        # se não achou, tenta achar por nome dos times
        if not db_game:
            db_game = find_db_game_by_team_names(conn, home_name, away_name)

        # se encontrou — actualizar
        if db_game:
            matched_games += 1
            conn.execute("""
                UPDATE games
                SET
                    api_game_id = ?,
                    game_datetime = ?,
                    score_home = ?,
                    score_away = ?
                WHERE id = ?
            """, (
                api_game_id,
                fixture_date,
                score_home,
                score_away,
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
    "Scotland": "Escócia"
}

def normalize_team_name(name):
    if not name:
        return ""

    translated_name = TEAM_NAME_MAP.get(name, name)

    return (
        translated_name.strip()
        .lower()
        .replace("á", "a")
        .replace("à", "a")
        .replace("â", "a")
        .replace("ã", "a")
        .replace("é", "e")
        .replace("ê", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ô", "o")
        .replace("õ", "o")
        .replace("ú", "u")
        .replace("ç", "c")
    )


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
# REGRAS DE PONTUAÇÃO
# =========================
def calculate_points(real_home, real_away, pred_home, pred_away):
    if pred_home is None or pred_away is None:
        return 0

    real_home = int(real_home)
    real_away = int(real_away)
    pred_home = int(pred_home)
    pred_away = int(pred_away)

    # Placar exato
    if real_home == pred_home and real_away == pred_away:
        return 10

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


def smart_sync():
    """Só sincroniza se houver jogos hoje ou durante a Copa 2026."""
    if not has_games_today():
        print(f"[sync] Sem jogos hoje ({datetime.utcnow().date()}) — sync ignorado")
        return

    print(f"[sync] Jogos hoje — a sincronizar às {datetime.now().strftime('%H:%M')}")
    try:
        result = sync_games_from_api()
        print(f"[sync] OK — {result['updated_games']} jogos actualizados, {result['skipped_games']} ignorados")
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
        country_code = request.form["country_code"]
        eureka_unit = request.form["eureka_unit"]

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
            return "Esse jogo já começou. Palpite bloqueado."

        conn.execute("""
            INSERT OR REPLACE INTO predictions
            (user_id, game_id, predicted_home_score, predicted_away_score, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            game_id,
            home_score,
            away_score,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()
        conn.close()

        return redirect(url_for("predict", saved_game=game_id))

    # -------------------
    # GET: listar jogos e palpites
    # -------------------
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
            p.predicted_away_score

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

        # Formatar data para exibição em hora de Lisboa
        try:
            dt_str_clean = raw_datetime.replace('T', ' ').replace('+00:00', '').replace('Z', '')
            dt_utc = datetime.fromisoformat(dt_str_clean)
            lisbon_tz = zoneinfo.ZoneInfo("Europe/Lisbon")
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            dt_lisbon = dt_utc.astimezone(lisbon_tz)
            game_datetime_display = dt_lisbon.strftime("%d/%m · %H:%M")
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
            "predicted_home_score": row["predicted_home_score"],
            "predicted_away_score": row["predicted_away_score"],
            "points": points,
            "score_home": real_home,
            "score_away": real_away,
            "is_locked": is_locked,
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
    if session.get("user_id") != 1:
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
