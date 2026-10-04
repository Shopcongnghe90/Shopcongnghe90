/* ZEUS Workbench — JS thuần (menu di động, sáng/tối, xác nhận từ chối). Không phụ thuộc bên ngoài. */
(function () {
  "use strict";
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("zwb-theme");
    if (saved) root.setAttribute("data-theme", saved);
  } catch (e) {}
  var themeBtn = document.getElementById("theme-toggle");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var dark = root.getAttribute("data-theme") === "dark" ||
        (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
      var next = dark ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("zwb-theme", next); } catch (e) {}
    });
  }
  var navBtn = document.getElementById("nav-toggle");
  if (navBtn) {
    navBtn.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      navBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  // Từ chối cần lý do: chặn gửi sớm ở phía trình duyệt (máy chủ vẫn kiểm tra lại).
  document.querySelectorAll("form.approval, .approval form").forEach(function (f) {
    f.addEventListener("submit", function (ev) {
      var btn = ev.submitter;
      var reason = f.querySelector("input[name=comment]");
      if (btn && btn.value === "reject" && reason && reason.value.trim().length < 3) {
        ev.preventDefault();
        reason.focus();
        reason.setCustomValidity("Vui lòng nhập lý do từ chối");
        reason.reportValidity();
        reason.addEventListener("input", function () { reason.setCustomValidity(""); }, { once: true });
      }
    });
  });
})();
