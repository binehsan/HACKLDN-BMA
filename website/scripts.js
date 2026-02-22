// Small UI niceties for the landing page
document.addEventListener('DOMContentLoaded', () => {
  const cta = document.querySelector('.btn-cta');
  if (cta) {
    cta.addEventListener('click', (e) => {
      e.preventDefault();
      const btn = e.currentTarget;
      btn.classList.add('pulse');
      setTimeout(() => btn.classList.remove('pulse'), 900);
      // scroll to get-started
      const target = document.querySelector('#get-started');
      if (target) target.scrollIntoView({behavior: 'smooth', block: 'center'});
    });
  }

  // fade in sections
  const obs = new IntersectionObserver((entries) => {
    entries.forEach(en => {
      if (en.isIntersecting) en.target.classList.add('visible');
    });
  }, {threshold: 0.15});
  document.querySelectorAll('section').forEach(s => obs.observe(s));
});
