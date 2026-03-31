// Scroll suave para âncoras — compensa a altura do menu fixo
document.querySelectorAll('a[href^="#"]').forEach((link) => {
  link.addEventListener("click", (e) => {
    const targetId = link.getAttribute("href").slice(1);
    const target = document.getElementById(targetId);
    if (!target) return;
    e.preventDefault();
    const menuEl = document.querySelector(".menu-sticky");
    const menuHeight = menuEl ? menuEl.offsetHeight : 0;
    const targetTop = target.getBoundingClientRect().top + window.scrollY - menuHeight;
    window.scrollTo({ top: targetTop, behavior: "smooth" });
  });
});
