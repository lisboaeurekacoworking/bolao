// ══════════════════════════════════════════
// PREDICT.JS — Palpites da Arena Eureka
// ══════════════════════════════════════════

// ── Tabs de fase ──
document.querySelectorAll(".predict-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.tab;
    document.querySelectorAll(".predict-tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".predict-stage-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`stage-${target}`).classList.add("active");
  });
});

// ── Subfiltros A-L dentro da primeira fase ──
document.querySelectorAll(".predict-group-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const stageId = btn.dataset.stage;
    const group = btn.dataset.group;
    document.querySelectorAll(`.predict-group-btn[data-stage="${stageId}"]`)
      .forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(`[data-stage-block="${stageId}"]`).forEach((block) => {
      block.style.display = (group === "all" || block.dataset.groupBlock === group) ? "block" : "none";
    });
  });
});

// ── Filtro global ──
const chips = document.querySelectorAll(".predict-chip");
const selecaoFilter = document.getElementById("selecao-filter");
const selecaoSelect = document.getElementById("selecao-select");
let activeFilter = "all";
let activeTeam = "";

chips.forEach((chip) => {
  chip.addEventListener("click", () => {
    chips.forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    activeFilter = chip.dataset.filter;
    if (selecaoFilter) selecaoFilter.style.display = activeFilter === "selecao" ? "flex" : "none";
    if (activeFilter !== "selecao") activeTeam = "";
    applyFilter();
  });
});

if (selecaoSelect) {
  selecaoSelect.addEventListener("change", () => {
    activeTeam = selecaoSelect.value;
    applyFilter();
  });
}

function getTodayStr() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")}`;
}

function applyFilter() {
  const today = getTodayStr();
  document.querySelectorAll(".match-card").forEach((card) => {
    let show = true;
    switch (activeFilter) {
      case "today":
        show = (card.dataset.datetime || "").trim().split(" ")[0] === today;
        break;
      case "missing":
        show = card.dataset.locked === "false" && card.dataset.hasPrediction === "false";
        break;
      case "done":
        show = card.dataset.locked === "false" && card.dataset.hasPrediction === "true";
        break;
      case "selecao":
        if (activeTeam) show = card.dataset.home === activeTeam || card.dataset.away === activeTeam;
        break;
      default: show = true;
    }
    card.classList.toggle("match-card--hidden", !show);
  });

  document.querySelectorAll(".predict-group-block").forEach((block) => {
    const visible = block.querySelectorAll(".match-card:not(.match-card--hidden)").length;
    block.classList.toggle("predict-group-block--hidden", visible === 0);
  });

  document.querySelectorAll(".predict-stage-panel").forEach((panel) => {
    const visible = panel.querySelectorAll(".match-card:not(.match-card--hidden)").length;
    let msg = panel.querySelector(".predict-stage-empty");
    if (!msg) {
      msg = document.createElement("p");
      msg.className = "predict-no-results predict-stage-empty";
      msg.textContent = "Nenhum jogo encontrado com este filtro.";
      panel.appendChild(msg);
    }
    msg.classList.toggle("visible", activeFilter !== "all" && visible === 0);
  });
}

// ── Toast ──
function showToast(msg, duration = 2800) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), duration);
}

// ── AJAX — salvar palpite sem reload ──
document.querySelectorAll(".ajax-predict-form").forEach((form) => {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = new FormData(form);
    const homeVal = data.get("home_score");
    const awayVal = data.get("away_score");
    const predictUrl = document.querySelector("main[data-predict-url]")?.dataset.predictUrl || window.location.href;

    try {
      const response = await fetch(predictUrl, { method: "POST", body: data });
      if (response.ok) {
        const btn = form.querySelector(".save-bet-btn");
        if (btn) btn.textContent = "Atualizar palpite";

        let savedBox = form.closest(".bet-center-stack")?.querySelector(".saved-bet-box");
        if (!savedBox) {
          savedBox = document.createElement("div");
          savedBox.className = "saved-bet-box";
          form.closest(".bet-center-stack")?.insertBefore(savedBox, form);
        }
        savedBox.innerHTML = `
          <span class="saved-bet-label">Palpite atual</span>
          <div class="saved-bet-score">
            <span>${homeVal}</span>
            <span class="score-separator">x</span>
            <span>${awayVal}</span>
          </div>`;
        form.reset();
        const card = form.closest(".match-card");
        if (card) card.dataset.hasPrediction = "true";
        showToast("✓ Palpite guardado!");
      } else {
        showToast("Erro ao guardar. Tenta novamente.");
      }
    } catch (err) {
      showToast("Erro de ligação. Tenta novamente.");
    }
  });
});

// ══════════════════════════════════════════
// POLLING DE RESULTADOS — verifica a cada 2 minutos
// se há resultados novos e atualiza só os cards afetados
// ══════════════════════════════════════════

// Guarda os resultados que já conhecemos para comparar
let knownResults = {};

// Inicializar com os resultados já presentes na página
document.querySelectorAll(".match-card[data-locked='true']").forEach((card) => {
  const id = card.id.replace("game-card-", "");
  const scoreEl = card.querySelector(".real-result-score");
  if (scoreEl) {
    const spans = scoreEl.querySelectorAll("span");
    if (spans.length >= 2) {
      knownResults[id] = { score_home: spans[0].textContent.trim(), score_away: spans[2]?.textContent.trim() };
    }
  }
});

async function pollResults() {
  try {
    const res = await fetch("/api/results");
    if (!res.ok) return;
    const results = await res.json();

    let updated = false;

    for (const [gameId, result] of Object.entries(results)) {
      const card = document.getElementById(`game-card-${gameId}`);
      if (!card) continue;

      const known = knownResults[gameId];
      const hasChanged = !known ||
        String(known.score_home) !== String(result.score_home) ||
        String(known.score_away) !== String(result.score_away);

      if (!hasChanged) continue;

      // Resultado novo ou alterado — atualizar o card
      knownResults[gameId] = result;
      updated = true;

      // Marcar como locked
      card.dataset.locked = "true";
      card.classList.add("match-card-locked");

      // Obter dados do palpite do próprio card para calcular acertos
      const predBox = card.querySelector(".saved-bet-score");
      let predHome = null, predAway = null;
      if (predBox) {
        const spans = predBox.querySelectorAll("span");
        predHome = parseInt(spans[0]?.textContent.trim());
        predAway = parseInt(spans[2]?.textContent.trim());
      }

      const betCenter = card.querySelector(".bet-center");
      if (!betCenter) continue;

      // Reconstruir o conteúdo do bet-center com resultado + análise
      betCenter.innerHTML = buildLockedCardHTML(
        predHome, predAway,
        result.score_home, result.score_away
      );
    }

    if (updated) {
      showToast("Resultados atualizados!");
      applyFilter(); // Re-aplicar filtro após atualização
    }
  } catch (err) {
    // Silencioso — não mostrar erro se o polling falhar
  }
}

// Construir HTML do card bloqueado com resultado e análise
function buildLockedCardHTML(predHome, predAway, realHome, realAway) {
  const hasPred = predHome !== null && !isNaN(predHome);

  // Caixa do palpite
  const predBox = hasPred
    ? `<div class="saved-bet-box">
        <span class="saved-bet-label">O teu palpite</span>
        <div class="saved-bet-score">
          <span>${predHome}</span>
          <span class="score-separator">x</span>
          <span>${predAway}</span>
        </div>
      </div>`
    : `<div class="saved-bet-box saved-bet-box--empty">
        <span class="saved-bet-label">Sem palpite</span>
        <div class="saved-bet-score saved-bet-score--empty">—</div>
      </div>`;

  // Caixa do resultado real
  const resultBox = `<div class="real-result-box">
    <span class="real-result-label">Resultado real</span>
    <div class="real-result-score">
      <span>${realHome}</span>
      <span class="real-result-separator">x</span>
      <span>${realAway}</span>
    </div>
  </div>`;

  // Tags de análise
  let tags = "";
  if (hasPred) {
    const points = calcPoints(predHome, predAway, realHome, realAway);

    if (predHome === realHome && predAway === realAway) {
      tags += `<span class="match-detail-tag tag-hit">✓ Placar exato</span>`;
    } else {
      const realRes = realHome > realAway ? "home" : realHome < realAway ? "away" : "draw";
      const predRes = predHome > predAway ? "home" : predHome < predAway ? "away" : "draw";
      tags += realRes === predRes
        ? `<span class="match-detail-tag tag-hit">✓ Vencedor</span>`
        : `<span class="match-detail-tag tag-miss">✗ Vencedor</span>`;
      tags += (predHome === realHome || predAway === realAway)
        ? `<span class="match-detail-tag tag-hit">✓ Um lado</span>`
        : `<span class="match-detail-tag tag-miss">✗ Placar</span>`;
    }
    tags += `<span class="match-detail-tag tag-points">${calcPoints(predHome, predAway, realHome, realAway)} pts</span>`;
  }

  const detailRow = hasPred ? `<div class="match-detail-row">${tags}</div>` : "";

  return `<div class="bet-center-stack">
    ${predBox}
    ${resultBox}
    ${detailRow}
  </div>`;
}

// Replicar a lógica de pontuação do backend em JS
// (mesma lógica do calculate_points no app.py)
function calcPoints(predHome, predAway, realHome, realAway) {
  if (predHome === realHome && predAway === realAway) return 10;
  const realRes = realHome > realAway ? "home" : realHome < realAway ? "away" : "draw";
  const predRes = predHome > predAway ? "home" : predHome < predAway ? "away" : "draw";
  const acertouVencedor = realRes === predRes;
  const acertouUmLado = predHome === realHome || predAway === realAway;
  if (acertouVencedor && acertouUmLado) return 7;
  if (acertouVencedor) return 5;
  if (acertouUmLado) return 2;
  return 0;
}

// Correr o polling a cada 2 minutos (120000ms)
// Porquê 2 min: o sync do servidor corre a cada 5 min,
// 2 min garante que apanhamos o resultado logo após o sync
setInterval(pollResults, 120000);
