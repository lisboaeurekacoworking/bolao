// ── Confirmação de apagar (post e comentário) ──
// Usa data-confirm para evitar problemas de aspas com i18n
document.querySelectorAll('.confirm-delete').forEach(form => {
  form.addEventListener('submit', e => {
    const msg = form.dataset.confirm || 'Are you sure?';
    if (!confirm(msg)) e.preventDefault();
  });
});

// ── Toast ──
function showToast(msg, duration = 2800) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), duration);
}

// ── Preservar scroll após submit de comentário ──
document.querySelectorAll('.bettalks-comment-form').forEach(form => {
  form.addEventListener('submit', () => {
    sessionStorage.setItem('bettalks-scroll', window.scrollY);
  });
});

window.addEventListener('load', () => {
  const saved = sessionStorage.getItem('bettalks-scroll');
  if (saved) {
    window.scrollTo(0, parseInt(saved));
    sessionStorage.removeItem('bettalks-scroll');
  }
});
