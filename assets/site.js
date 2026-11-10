/* What every page shares: the nav's hairline and phone menu, the skyline
   (drawn from /assets/skyline.svg so it can rise when it comes into view),
   the sky drifting as the page scrolls, and sections rising as they are
   reached. All of it is decoration over a page that is whole without it. */
(function () {
  var root = document.documentElement;
  var moves = root.classList.contains('anim');   // set in the head unless reduced motion was asked for
  root.classList.add('js');

  // ── Nav ──
  var nav = document.getElementById('nav');
  var burger = document.getElementById('navBurger');
  var links = document.getElementById('navLinks');
  if (burger && links) {
    var setMenu = function (open) {
      links.classList.toggle('open', open);
      burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    burger.addEventListener('click', function () { setMenu(!links.classList.contains('open')); });
    links.addEventListener('click', function (e) { if (e.target.closest('a')) setMenu(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setMenu(false); });
  }

  // ── The sky drifts more slowly than the page (one write a frame) ──
  var sky = document.querySelector('.sky');
  var queued = false;
  function onScroll() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () {
      queued = false;
      var y = window.scrollY || 0;
      if (nav) nav.classList.toggle('scrolled', y > 12);
      if (sky && moves) {
        var h = sky.offsetHeight || 1;
        sky.style.setProperty('--p', Math.max(0, Math.min(1, y / h)).toFixed(3));
      }
    });
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ── Rise when seen ──
  var seen = function (el) { el.classList.add('in'); };
  var watch = seen;
  if (moves && 'IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { seen(e.target); io.unobserve(e.target); }
      });
    }, { threshold: 0.18, rootMargin: '0px 0px -6% 0px' });
    watch = function (el) { io.observe(el); };
  }
  [].forEach.call(document.querySelectorAll('.reveal'), watch);

  // ── The skyline ──
  [].forEach.call(document.querySelectorAll('.skyline[data-src]'), function (box) {
    fetch(box.getAttribute('data-src'))
      .then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (svg) {
        if (svg.indexOf('<svg') !== 0) return;
        box.innerHTML = svg;               // our own file, same origin
        // Let the first frame paint the stelae lying flat, then let them rise.
        requestAnimationFrame(function () { requestAnimationFrame(function () { watch(box); }); });
      })
      .catch(function () { /* the sky is still a sky without it */ });
  });
})();
