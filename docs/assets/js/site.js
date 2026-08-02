(function () {
  'use strict';

  var THEME_KEY = 'mq-theme';
  var root = document.documentElement;

  /* --- Colour scheme ------------------------------------------------ */

  var toggle = document.getElementById('theme-toggle');
  if (toggle) {
    toggle.addEventListener('click', function () {
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
      if (window.__mqRenderMermaid) { window.__mqRenderMermaid(next); }
    });
  }

  var prose = document.getElementById('prose');
  if (!prose) { return; }

  /* --- Heading anchors ---------------------------------------------- */

  var headings = prose.querySelectorAll('h2[id], h3[id], h4[id]');

  Array.prototype.forEach.call(headings, function (h) {
    var a = document.createElement('a');
    a.className = 'anchor';
    a.href = '#' + h.id;
    a.setAttribute('aria-label', 'Link to this section');
    a.textContent = '#';
    h.appendChild(a);
  });

  /* --- Table of contents -------------------------------------------- */

  var toc = document.getElementById('toc');
  var tocLinks = [];

  if (toc) {
    var tocTargets = prose.querySelectorAll('h2[id], h3[id]');

    Array.prototype.forEach.call(tocTargets, function (h) {
      var link = document.createElement('a');
      link.href = '#' + h.id;
      link.className = 'lvl-' + h.tagName.charAt(1);
      // The injected anchor is a child of the heading, so read the text before it.
      link.textContent = (h.firstChild && h.firstChild.textContent || h.textContent).trim();
      toc.appendChild(link);
      tocLinks.push({ link: link, target: h });
    });

    if (!tocLinks.length) {
      var sidebar = document.getElementById('doc-sidebar');
      if (sidebar) { sidebar.style.display = 'none'; }
    }
  }

  /* --- Scrollspy ----------------------------------------------------- */

  if (tocLinks.length) {
    var header = document.querySelector('.site-header');
    var active = null;
    var ticking = false;

    var mark = function () {
      ticking = false;

      // The active section is the last heading to have crossed a line just
      // below the sticky header. Headings come back in document order, so the
      // first one still below the line ends the scan.
      var line = (header ? header.offsetHeight : 60) + 24;
      var current = tocLinks[0];

      for (var i = 0; i < tocLinks.length; i++) {
        if (tocLinks[i].target.getBoundingClientRect().top > line) { break; }
        current = tocLinks[i];
      }

      // A final section shorter than the viewport never crosses the line, so
      // hitting the bottom of the page awards it outright.
      var doc = document.documentElement;
      if (window.innerHeight + window.scrollY >= doc.scrollHeight - 2) {
        current = tocLinks[tocLinks.length - 1];
      }

      if (current === active) { return; }
      active = current;

      tocLinks.forEach(function (entry) {
        entry.link.classList.toggle('active', entry === current);
      });

      // Keep the lit entry in view when the list is taller than its own box.
      if (toc.scrollHeight > toc.clientHeight) {
        var lt = current.link.offsetTop;
        if (lt < toc.scrollTop || lt > toc.scrollTop + toc.clientHeight - 40) {
          toc.scrollTop = lt - toc.clientHeight / 2;
        }
      }
    };

    var onScroll = function () {
      if (ticking) { return; }
      ticking = true;
      window.requestAnimationFrame(mark);
    };

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll, { passive: true });
    mark();
  }

  /* --- Wide tables scroll inside their own box ----------------------- */

  Array.prototype.forEach.call(prose.querySelectorAll('table'), function (table) {
    if (table.parentNode.classList.contains('table-scroll')) { return; }
    var box = document.createElement('div');
    box.className = 'table-scroll';
    table.parentNode.insertBefore(box, table);
    box.appendChild(table);
  });

  /* --- Mermaid, loaded only when a page actually has a diagram ------- */

  var diagrams = prose.querySelectorAll('code.language-mermaid, pre > code.language-mermaid');
  if (!diagrams.length) { return; }

  var sources = [];

  Array.prototype.forEach.call(diagrams, function (code) {
    var pre = code.closest('pre') || code.parentNode;
    var holder = pre.closest('.highlighter-rouge, figure.highlight') || pre;
    var box = document.createElement('div');
    box.className = 'mermaid-wrap';
    holder.parentNode.replaceChild(box, holder);
    sources.push({ box: box, text: code.textContent });
  });

  var script = document.createElement('script');
  script.type = 'module';
  script.textContent = [
    "import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs';",
    "window.__mqMermaid = mermaid;",
    "window.dispatchEvent(new Event('mq-mermaid-ready'));"
  ].join('\n');

  window.addEventListener('mq-mermaid-ready', function () {
    var mermaid = window.__mqMermaid;
    var counter = 0;

    var render = function (theme) {
      mermaid.initialize({
        startOnLoad: false,
        theme: 'base',
        securityLevel: 'strict',
        fontFamily: 'var(--font-mono)',
        themeVariables: theme === 'light'
          ? {
              background: '#ffffff',
              primaryColor: '#f1f3fa',
              primaryTextColor: '#12142a',
              primaryBorderColor: '#7c3aed',
              lineColor: '#0b74b8',
              secondaryColor: '#eef2ff',
              tertiaryColor: '#f7f8fc',
              clusterBkg: '#f7f8fc',
              clusterBorder: '#dfe3f0'
            }
          : {
              background: '#10101f',
              primaryColor: '#16162a',
              primaryTextColor: '#dfe2f2',
              primaryBorderColor: '#a855f7',
              lineColor: '#4cc9f0',
              secondaryColor: '#12121f',
              tertiaryColor: '#0b0b17',
              clusterBkg: '#0b0b17',
              clusterBorder: '#23233d'
            }
      });

      sources.forEach(function (item) {
        counter += 1;
        mermaid.render('mq-diagram-' + counter, item.text).then(function (out) {
          item.box.innerHTML = out.svg;
        }).catch(function () {
          // A diagram that will not parse costs the diagram, not the page.
          item.box.innerHTML = '<pre>' + item.text.replace(/[<>&]/g, function (c) {
            return { '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c];
          }) + '</pre>';
        });
      });
    };

    window.__mqRenderMermaid = render;
    render(root.getAttribute('data-theme'));
  });

  document.body.appendChild(script);
})();
