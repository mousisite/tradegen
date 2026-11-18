/* Orenth inside the iPhone app.
 *
 * The server sends this only when the app's User-Agent asks for the page, and
 * it does nothing at all unless Capacitor says this really is the native app.
 * It runs on every page load, because the app forgets every listener each
 * time a new page loads.
 *
 * What it does:
 *   - window.OrenthNative, for other scripts: webAuth, appleSignIn, share,
 *     haptic (see each one below).
 *   - Links to other sites open in a sheet over the app (the in-app browser)
 *     instead of leaving it, so the app never browses the open web.
 *   - The share buttons open the phone's own share sheet.
 *   - The status bar takes the page's own light or dark colours.
 *   - The launch screen goes away as soon as the page is ready.
 *   - app.orenth.ios://open?path=/monitor opens that page of this site.
 *   - window.OrenthNotify.onAppStart(), if the notifications script is there.
 *
 * No bundler: plugins are called through Capacitor.nativePromise, which the
 * app injects into every page it shows.
 */
(function () {
  'use strict';

  var C = window.Capacitor;
  if (!C || typeof C.isNativePlatform !== 'function' || !C.isNativePlatform()) {
    return;
  }

  var SCHEME = 'app.orenth.ios';
  var LIGHT_GROUND = '#f4f3ee';
  var DARK_GROUND = '#191817';

  function noop() {}

  function failure(code, message) {
    var e = new Error(message || code);
    e.code = code;
    return e;
  }

  function has(name) {
    if (typeof C.isPluginAvailable === 'function') {
      return C.isPluginAvailable(name);
    }
    return !!(C.Plugins && C.Plugins[name]);
  }

  function call(name, method, options) {
    if (typeof C.nativePromise !== 'function') {
      return Promise.reject(failure('UNAVAILABLE', name + ' is not available.'));
    }
    try {
      return C.nativePromise(name, method, options || {});
    } catch (e) {
      return Promise.reject(e);
    }
  }

  // A listener on a plugin event. Capacitor's own bridge returns the handle
  // straight away; the packaged library returns a promise of it. Both are
  // handled, and the result always has a remove().
  function listen(name, eventName, callback) {
    var plugin = C.Plugins && C.Plugins[name];
    var handle = null;
    var removed = false;
    if (!plugin || typeof plugin.addListener !== 'function') {
      return { remove: noop };
    }
    try {
      var got = plugin.addListener(eventName, callback);
      if (got && typeof got.then === 'function') {
        got.then(function (h) {
          handle = h;
          if (removed && h && typeof h.remove === 'function') { h.remove(); }
        }, noop);
      } else {
        handle = got;
      }
    } catch (e) {
      return { remove: noop };
    }
    return {
      remove: function () {
        removed = true;
        if (handle && typeof handle.remove === 'function') {
          try { handle.remove(); } catch (e) { /* already gone */ }
        }
      }
    };
  }

  // One word for "the person closed it", whichever plugin said so. Always a
  // new Error: a browser's own errors have a read-only code.
  function normalise(err) {
    var code = (err && typeof err.code === 'string') ? err.code : '';
    var message = String((err && err.message) || err || 'It did not work.');
    var cancelled = code === 'CANCELLED' || code === 'SIGN_IN_CANCELED' ||
      !!(err && err.name === 'AbortError') || /cancel/i.test(message);
    var out = failure(cancelled ? 'CANCELLED' : (code || 'FAILED'), message);
    if (code && code !== out.code) { out.nativeCode = code; }
    return out;
  }

  // ---------------------------------------------------------------------------
  // Same-site paths, checked strictly: one leading slash, no scheme, nothing
  // that a browser would read as another host.
  // ---------------------------------------------------------------------------
  function safePath(path) {
    if (typeof path !== 'string' || !path || path.length > 1024) { return null; }
    if (path.charAt(0) !== '/' || path.charAt(1) === '/') { return null; }
    if (/[\\\s\u0000-\u001f\u007f]/.test(path)) { return null; }
    var resolved;
    try { resolved = new URL(path, location.origin); } catch (e) { return null; }
    if (resolved.origin !== location.origin) { return null; }
    return resolved.pathname + resolved.search + resolved.hash;
  }

  // app.orenth.ios://open?path=/monitor gives "/monitor"; anything else, null.
  function deepLinkPath(raw) {
    if (typeof raw !== 'string' || raw.length > 2048) { return null; }
    var u;
    try { u = new URL(raw); } catch (e) { return null; }
    if (u.protocol !== SCHEME + ':' || u.host !== 'open') { return null; }
    return safePath(u.searchParams.get('path'));
  }

  // A sign-in answer: this app's scheme, anything but the "open" links.
  function isOurCallback(raw) {
    if (typeof raw !== 'string' || raw.length > 4096) { return false; }
    var u;
    try { u = new URL(raw); } catch (e) { return false; }
    return u.protocol === SCHEME + ':' && u.host !== 'open';
  }

  // ---------------------------------------------------------------------------
  // The API other scripts use. Every method returns a promise, and every
  // failure carries a .code; "CANCELLED" means the person closed the sheet.
  // ---------------------------------------------------------------------------

  // Opens an address on this site in Apple's sign-in sheet, which shares
  // nothing with the app's own pages and hands the answer back only to this
  // app. Resolves with the full app.orenth.ios://... address the site finally
  // sent the sheet to.
  function webAuth(url) {
    var target;
    try { target = new URL(url, location.href); } catch (e) {
      return Promise.reject(failure('INVALID_URL', 'Not an address.'));
    }
    if (target.protocol !== 'https:' || target.host !== location.host) {
      return Promise.reject(failure('INVALID_URL',
        'Sign-in must start on this site, over https.'));
    }
    if (has('WebAuth')) {
      return call('WebAuth', 'start', { url: target.href, callbackScheme: SCHEME })
        .then(function (result) {
          if (result && typeof result.url === 'string' && result.url) {
            return result.url;
          }
          throw failure('FAILED', 'The sign-in sheet returned no address.');
        }, function (err) { throw normalise(err); });
    }
    // An older app without the sign-in sheet: the in-app browser, and the
    // callback arriving as an app link.
    if (!has('Browser') || !has('App')) {
      return Promise.reject(failure('UNAVAILABLE', 'Sign-in is not available in this version of the app.'));
    }
    return new Promise(function (resolve, reject) {
      var settled = false;
      var opened = null;
      var closed = null;
      function settle(ok, value) {
        if (settled) { return; }
        settled = true;
        if (opened) { opened.remove(); }
        if (closed) { closed.remove(); }
        if (ok) { resolve(value); } else { reject(value); }
      }
      opened = listen('App', 'appUrlOpen', function (event) {
        var u = event && event.url;
        if (isOurCallback(u)) {
          call('Browser', 'close', {}).catch(noop);
          settle(true, u);
        }
      });
      closed = listen('Browser', 'browserFinished', function () {
        settle(false, failure('CANCELLED', 'Sign-in was cancelled.'));
      });
      call('Browser', 'open', { url: target.href }).catch(function (err) {
        settle(false, normalise(err));
      });
    });
  }

  // Native Sign in with Apple. The nonce is passed to Apple unchanged and
  // comes back inside the identity token for the server to check.
  function appleSignIn(nonce) {
    if (typeof nonce !== 'string' || !nonce) {
      return Promise.reject(failure('INVALID_NONCE', 'A nonce from the server is needed.'));
    }
    if (!has('AppleSignIn')) {
      return Promise.reject(failure('UNAVAILABLE', 'Sign in with Apple is not available in this version of the app.'));
    }
    return call('AppleSignIn', 'signIn', { scopes: ['EMAIL', 'FULL_NAME'], nonce: nonce })
      .then(function (r) {
        r = r || {};
        return {
          identityToken: r.idToken || '',
          authorizationCode: r.authorizationCode || '',
          givenName: r.givenName || null,
          familyName: r.familyName || null,
          email: r.email || null
        };
      }, function (err) { throw normalise(err); });
  }

  // The phone's share sheet. Resolves with what Apple says was picked.
  function share(options) {
    options = options || {};
    var out = {};
    if (options.title) { out.title = String(options.title); }
    if (options.text) { out.text = String(options.text); }
    if (options.url) { out.url = String(options.url); }
    if (!out.text && !out.url) {
      return Promise.reject(failure('INVALID', 'Nothing to share.'));
    }
    if (has('Share')) {
      return call('Share', 'share', out).then(function (r) { return r || {}; },
        function (err) { throw normalise(err); });
    }
    if (navigator.share) {
      return navigator.share(out).then(function () { return {}; },
        function (err) { throw normalise(err); });
    }
    return Promise.reject(failure('UNAVAILABLE', 'Sharing is not available.'));
  }

  // A small tap felt in the hand. kind: light, medium, heavy, success,
  // warning, error or selection. Never fails.
  function haptic(kind) {
    if (!has('Haptics')) { return Promise.resolve(); }
    var k = String(kind || 'light').toLowerCase();
    var p;
    if (k === 'success' || k === 'warning' || k === 'error') {
      p = call('Haptics', 'notification', { type: k.toUpperCase() });
    } else if (k === 'selection') {
      p = call('Haptics', 'selectionStart', {})
        .then(function () { return call('Haptics', 'selectionChanged', {}); })
        .then(function () { return call('Haptics', 'selectionEnd', {}); });
    } else {
      var style = (k === 'medium' || k === 'heavy') ? k.toUpperCase() : 'LIGHT';
      p = call('Haptics', 'impact', { style: style });
    }
    return p.then(noop, noop);
  }

  window.OrenthNative = {
    available: true,
    scheme: SCHEME,
    webAuth: webAuth,
    appleSignIn: appleSignIn,
    share: share,
    haptic: haptic,
    appStarted: false
  };

  // ---------------------------------------------------------------------------
  // Links. Another site opens in the in-app browser sheet; this site stays in
  // the app's own view even when the link asks for a new window, which the
  // app would otherwise hand to Safari. A download opens in the sheet too, so
  // the page itself never turns into a bare picture.
  // ---------------------------------------------------------------------------
  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0) { return; }
    var start = e.target;
    var a = start && start.closest ? start.closest('a[href]') : null;
    if (!a) { return; }
    var url;
    try { url = new URL(a.getAttribute('href'), location.href); } catch (err) { return; }
    if (url.protocol !== 'https:' && url.protocol !== 'http:') { return; }
    if (url.host !== location.host) {
      e.preventDefault();
      call('Browser', 'open', { url: url.href }).catch(noop);
      return;
    }
    if (a.hasAttribute('download')) {
      e.preventDefault();
      call('Browser', 'open', { url: url.href }).catch(noop);
      return;
    }
    if ((a.getAttribute('target') || '').toLowerCase() === '_blank') {
      e.preventDefault();
      location.assign(url.href);
    }
  });

  // ---------------------------------------------------------------------------
  // Share buttons: the bar on a shared call and the invite link on the
  // account page. Caught before the page's own handler, which would use the
  // browser's share instead.
  // ---------------------------------------------------------------------------
  document.addEventListener('click', function (e) {
    var start = e.target;
    var button = start && start.closest ? start.closest('.sharebar [data-act="share"]') : null;
    if (!button) { return; }
    var bar = button.closest('.sharebar');
    var link = bar && bar.getAttribute('data-url');
    if (!link) { return; }
    e.preventDefault();
    e.stopPropagation();
    share({ title: 'Orenth', text: bar.getAttribute('data-text') || '', url: link })
      .then(function () { haptic('success'); }, function (err) {
        if (err && err.code === 'CANCELLED') { return; }
        var said = bar.querySelector('[data-said]');
        if (said) { said.textContent = link; }
      });
  }, true);

  // ---------------------------------------------------------------------------
  // Status bar: the same ground as the page, light or dark, and dark text on a
  // light ground. Read from the page itself, so it follows whatever the CSS
  // decided, and checked again when the phone switches appearance.
  // ---------------------------------------------------------------------------
  function hex(n) {
    var s = Math.max(0, Math.min(255, Math.round(n))).toString(16);
    return s.length === 1 ? '0' + s : s;
  }

  function pageGround() {
    var dark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var fallback = dark ? DARK_GROUND : LIGHT_GROUND;
    var node = document.body || document.documentElement;
    var colour = '';
    try { colour = window.getComputedStyle(node).backgroundColor || ''; } catch (e) { colour = ''; }
    var m = colour.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?/);
    if (!m || (m[4] !== undefined && parseFloat(m[4]) === 0)) { return fallback; }
    return '#' + hex(+m[1]) + hex(+m[2]) + hex(+m[3]);
  }

  function isDarkGround(colour) {
    var r = parseInt(colour.substr(1, 2), 16);
    var g = parseInt(colour.substr(3, 2), 16);
    var b = parseInt(colour.substr(5, 2), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128;
  }

  var lastGround = '';

  function syncStatusBar() {
    if (!has('StatusBar')) { return; }
    var ground = pageGround();
    if (ground === lastGround) { return; }
    lastGround = ground;
    // DARK means light text, for a dark ground; LIGHT means dark text.
    call('StatusBar', 'setStyle', { style: isDarkGround(ground) ? 'DARK' : 'LIGHT' }).catch(noop);
    call('StatusBar', 'setBackgroundColor', { color: ground }).catch(noop);
  }

  if (window.matchMedia) {
    var scheme = window.matchMedia('(prefers-color-scheme: dark)');
    if (typeof scheme.addEventListener === 'function') {
      scheme.addEventListener('change', syncStatusBar);
    } else if (typeof scheme.addListener === 'function') {
      scheme.addListener(syncStatusBar);
    }
  }
  if (window.MutationObserver) {
    new MutationObserver(syncStatusBar).observe(document.documentElement,
      { attributes: true, attributeFilter: ['data-theme', 'class'] });
  }

  // ---------------------------------------------------------------------------
  // App links: app.orenth.ios://open?path=/monitor. A link that started the
  // app is held by the app until this listener is in place.
  // ---------------------------------------------------------------------------
  listen('App', 'appUrlOpen', function (event) {
    var path = deepLinkPath(event && event.url);
    if (path !== null) { location.assign(path); }
  });

  // ---------------------------------------------------------------------------
  // Once the page is ready: colours, launch screen, notifications.
  // ---------------------------------------------------------------------------
  function ready() {
    syncStatusBar();
    if (has('SplashScreen')) {
      call('SplashScreen', 'hide', { fadeOutDuration: 200 }).catch(noop);
    }
    window.OrenthNative.appStarted = true;
    var notify = window.OrenthNotify;
    if (notify && typeof notify.onAppStart === 'function') {
      try { notify.onAppStart(); } catch (e) { /* its own problem, not the page's */ }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', ready);
  } else {
    ready();
  }
})();
