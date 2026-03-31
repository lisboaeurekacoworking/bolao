// MENU MOBILE
const menu = document.querySelector(".menu-toggle");
const menuLista = document.querySelector(".nav-menu");

if (menu && menuLista) {
  menu.addEventListener("click", () => {
    menuLista.classList.toggle("active");
  });
}

// GALERIA / MODAL
document.addEventListener("DOMContentLoaded", function () {
  const modal = document.getElementById("imageModal");
  const modalImg = document.getElementById("modalImage");
  const closeBtn = document.querySelector(".close");
  const galleryImages = document.querySelectorAll(".gallery-img");

  if (modal && modalImg && closeBtn && galleryImages.length > 0) {
    galleryImages.forEach((img) => {
      img.addEventListener("click", function () {
        modal.style.display = "block";
        modalImg.src = this.src;
      });
    });

    closeBtn.addEventListener("click", function () {
      modal.style.display = "none";
    });

    modal.addEventListener("click", function (event) {
      if (event.target === modal) {
        modal.style.display = "none";
      }
    });
  }
});

//scroll-anima

const hiddenElements = document.querySelectorAll(".hidden");

if (hiddenElements.length > 0) {
  const myObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) {
        entry.target.classList.add("show");
      } else {
        entry.target.classList.remove("show");
      }
    });
  });

  hiddenElements.forEach((element) => {
    myObserver.observe(element);
  });
}

// ── Menu activo —
(function () {
  const path = window.location.pathname;
  document.querySelectorAll(".li-menu a").forEach((link) => {
    if (link.classList.contains("lang-btn")) return;
    if (link.closest(".login-landing")) return;
    const href = link.getAttribute("href");
    if (!href || href.startsWith("#")) return;
    if (path === href || path.startsWith(href + "/")) {
      link.classList.add("active");
    }
  });
})();
