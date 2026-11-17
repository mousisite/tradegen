/* Signing in from inside the Orenth iPhone app.
 *
 * Google refuses to sign anyone in inside an app's own web view, and Apple's
 * page would open in Safari, outside the app. So in the app the Google and
 * Apple buttons open the sign-in in the system browser sheet instead. That
 * sheet keeps cookies of its own, so the server does not sign the sheet in:
 * it hands back a one-time code through the app's own address,
 * app.orenth.ios://auth/finish, and this page trades the code for a session
 * here, together with a secret that only this page held. A code caught by
 * another app on the phone is useless without that secret.
 *
 * When the app has Apple's own sign-in plugin, the Apple button uses the
 * system's Apple sheet, with Face ID, and no browser at all.
 *
 * Outside the app this does nothing: window.Capacitor does not exist there.
 */
(function () {
  'use strict';

  var cap = window.Capacitor;
  if (!cap || !cap.isNativePlatform || !cap.isNativePlatform()) { return; }
  var plugins = cap.Plugins || {};
  var Browser = plugins.Browser;
  var App = plugins.App;
  var box = document.querySelector('[data-app-signin]');
  if (!box) { return; }

  var FINISH = 'app.orenth.ios://auth/finish';
  var KEY = 'orenth-app-signin';
  var said = document.querySelector('[data-app-said]');
  var WORDS = {
    cancelled: 'Signing in was cancelled.',
    expired: 'That sign-in took too long. Try again.',
    refused: 'That account cannot be signed in to here. Try another way in.',
    failed: 'Signing in did not finish. Try again.',
    update: 'Update the Orenth app to sign in this way.'
  };

  function say(text) {
    if (!said) { return; }
    said.textContent = text;
    said.hidden = false;
  }

  function word(reason) {
    return Object.prototype.hasOwnProperty.call(WORDS, reason)
      ? WORDS[reason] : WORDS.failed;
  }

  function b64url(bytes) {
    var text = '';
    for (var i = 0; i < bytes.length; i++) { text += String.fromCharCode(bytes[i]); }
    return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function random(n) {
    return b64url(window.crypto.getRandomValues(new Uint8Array(n)));
  }

  // The secret the server will ask for, kept in this page's own session and
  // nowhere else. The sheet never sees it, only its SHA-256.
  function keep(pending) {
    try { window.sessionStorage.setItem(KEY, JSON.stringify(pending)); } catch (e) {}
  }
  function kept() {
    try { return JSON.parse(window.sessionStorage.getItem(KEY) || 'null'); }
    catch (e) { return null; }
  }
  function forget() {
    try { window.sessionStorage.removeItem(KEY); } catch (e) {}
  }

  function nextOf(link) {
    try {
      return new URL(link.getAttribute('href'), location.href).searchParams.get('next') || '';
    } catch (e) { return ''; }
  }

  // Google, or Apple without its plugin: the system browser sheet.
  function inSheet(link) {
    if (!Browser || !window.crypto || !window.crypto.subtle || !window.TextEncoder) {
      say(WORDS.update);
      return;
    }
    var verifier = random(32);
    var start = new URL(link.getAttribute('href'), location.href);
    window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
      .then(function (hash) {
        start.searchParams.set('app', '1');
        start.searchParams.set('challenge', b64url(new Uint8Array(hash)));
        keep({ verifier: verifier, next: start.searchParams.get('next') || '' });
        return Browser.open({ url: start.href, presentationStyle: 'popover' });
      })
      .catch(function () { forget(); say(WORDS.failed); });
  }

  // What the sheet hands back: FINISH?code=... or FINISH?error=<a word>.
  // quietly: only act when this page started a sign-in, for an address the
  // app was opened with, which can be an old one.
  function finish(url, quietly) {
    if (typeof url !== 'string' || url.indexOf(FINISH) !== 0) { return; }
    var pending = kept();
    if (!pending && quietly) { return; }
    forget();
    if (Browser) {
      // Closed straight away, before this page moves on. It may already be
      // gone, which is not worth saying.
      try {
        var closing = Browser.close();
        if (closing && closing.catch) { closing.catch(function () {}); }
      } catch (e) {}
    }
    var params = new URLSearchParams((url.split('?')[1] || '').split('#')[0]);
    var code = params.get('code');
    if (!code) { say(word(params.get('error'))); return; }
    if (!pending || !pending.verifier) { say(WORDS.expired); return; }
    // A plain form post, so the answer is a page this web view simply opens,
    // signed in, at the page the person was on their way to.
    var form = document.createElement('form');
    form.method = 'post';
    form.action = box.getAttribute('data-finish');
    [['code', code], ['verifier', pending.verifier], ['next', pending.next || '']]
      .forEach(function (pair) {
        var field = document.createElement('input');
        field.type = 'hidden';
        field.name = pair[0];
        field.value = pair[1];
        form.appendChild(field);
      });
    document.body.appendChild(form);
    form.submit();
  }

  // Apple's own sheet, through @capacitor-community/apple-sign-in. The nonce
  // comes from the server, which keeps its hash in this session; Apple signs
  // it into the token, so the token is good for this session and once.
  function withApple(link) {
    var plugin = plugins.SignInWithApple;
    var next = nextOf(link);
    fetch(box.getAttribute('data-apple-nonce'), {
      credentials: 'same-origin', headers: { 'Accept': 'application/json' }
    }).then(function (answer) {
      if (!answer.ok) { throw new Error('no nonce'); }
      return answer.json();
    }).then(function (got) {
      return plugin.authorize({
        clientId: link.getAttribute('data-client'),
        redirectURI: box.getAttribute('data-apple-redirect'),
        scopes: 'email name',
        state: random(16),
        nonce: got.nonce
      });
    }).then(function (result) {
      var r = (result && result.response) || {};
      return fetch(box.getAttribute('data-apple-native'), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({
          identityToken: r.identityToken || '',
          authorizationCode: r.authorizationCode || '',
          givenName: r.givenName || '',
          familyName: r.familyName || '',
          email: r.email || '',
          next: next
        })
      }).then(function (answer) {
        return answer.json().catch(function () { return {}; });
      });
    }).then(function (body) {
      if (body && body.ok) { location.assign(body.next || '/'); return; }
      say((body && body.error) || WORDS.failed);
    }).catch(function (err) {
      // Closing Apple's sheet is error 1001, "canceled", on iOS.
      var what = String((err && (err.code || err.message)) || '');
      say(/1001|cancel/i.test(what) ? WORDS.cancelled : WORDS.failed);
    });
  }

  box.addEventListener('click', function (e) {
    var link = e.target && e.target.closest ? e.target.closest('a[data-app-via]') : null;
    if (!link || !box.contains(link)) { return; }
    e.preventDefault();
    if (said) { said.hidden = true; }
    var apple = link.getAttribute('data-app-via') === 'apple';
    if (apple && plugins.SignInWithApple && link.getAttribute('data-native') === '1'
        && box.getAttribute('data-apple-native')) {
      withApple(link);
    } else if (apple && link.getAttribute('data-web') !== '1') {
      say(WORDS.update);
    } else {
      inSheet(link);
    }
  });

  if (App && App.addListener) {
    App.addListener('appUrlOpen', function (event) { finish(event && event.url, false); });
  }
  // iOS may stop the app while the sheet is up; then the address opens the
  // app afresh, and arrives as the address it was launched with.
  if (App && App.getLaunchUrl) {
    Promise.resolve().then(function () { return App.getLaunchUrl(); })
      .then(function (launched) {
        if (launched && launched.url) { finish(launched.url, true); }
      })
      .catch(function () {});
  }
})();
