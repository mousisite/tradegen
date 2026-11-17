/* Notifications on this device.

   When an alert fires, the server sends a notification to every device its
   owner switched on, even with no Orenth page open. In a browser that is Web
   Push, through the service worker at /sw.js and the push service the
   browser already uses. Inside the Orenth iPhone app it is the app's own
   notifications, through Apple, reached with window.Capacitor.

     window.OrenthNotify.supported()  whether this device can be sent them
     window.OrenthNotify.status()     a Promise of 'on', 'off', 'blocked'
                                      or 'unsupported'
     window.OrenthNotify.enable()     ask, subscribe and tell the server;
                                      call it from a tap, nothing else may
     window.OrenthNotify.disable()    stop, and have the server forget it
     window.OrenthNotify.test()       send "Notifications are on" here
     window.OrenthNotify.why()        a plain sentence about the last answer
     window.OrenthNotify.mine()       whether they are on here for the
                                      account signed in now

   A device belongs to one account. Once a visit this checks the device
   against the server: still this account's, it is kept up to date; turned
   off elsewhere, removed from another device, or forgotten when its sign-in
   ended, it is turned off here too; turned on by another account than the
   one signed in now, it is let go, so a shared phone never shows the last
   person's alerts. Nothing here asks a question unprompted. Each
   [data-notify] card (templates/_notify.html) is wired to these. */
(function () {
  'use strict';

  var CONFIG = '/api/push/config';
  var SUBSCRIBE = '/api/push/subscribe';
  var UNSUBSCRIBE = '/api/push/unsubscribe';
  var TEST = '/api/push/test';
  var WORKER = '/sw.js';
  // Which account switched notifications on in this browser or app.
  var OWNER = 'orenth-push-owner';
  var TOKEN = 'orenth-apns-token';

  var script = document.currentScript;
  var me = (script && script.getAttribute('data-user')) || '';
  // Changes with every sign-in, so each sign-in checks the device once.
  var visit = 'orenth-push-checked-' + me + '-'
    + ((script && script.getAttribute('data-mark')) || '');
  var reason = '';

  var SAID = {
    homeScreen: 'On an iPhone or iPad, notifications work once Orenth is on '
      + 'your Home Screen: tap Share, then Add to Home Screen, and open Orenth '
      + 'from there.',
    noBrowser: 'This browser cannot show notifications from a website.',
    blockedWeb: 'This browser has been told not to show Orenth’s '
      + 'notifications. Allow them for this site in the browser’s '
      + 'settings, then come back here.',
    blockedApp: 'Notifications are switched off for Orenth in this iPhone’s '
      + 'Settings. Turn them on under Settings, Notifications, Orenth.',
    serverOff: 'Notifications are not available here yet.',
    stale: 'These were set up with an older key. Turn them on again to keep '
      + 'them coming.'
  };

  function read(key) {
    try { return window.localStorage.getItem(key) || ''; } catch (e) { return ''; }
  }
  function write(key, value) {
    try {
      if (value) { window.localStorage.setItem(key, value); }
      else { window.localStorage.removeItem(key); }
    } catch (e) {}
  }
  // True the first time in this browser session.
  function firstThisVisit(key) {
    try {
      if (window.sessionStorage.getItem(key)) { return false; }
      window.sessionStorage.setItem(key, '1');
    } catch (e) {}
    return true;
  }

  // --- where this is running -------------------------------------------

  function app() {
    var cap = window.Capacitor;
    return !!(cap && typeof cap.isNativePlatform === 'function' && cap.isNativePlatform());
  }
  function native() {
    var cap = window.Capacitor;
    return (app() && cap.Plugins && cap.Plugins.PushNotifications) || null;
  }
  function appleTouch() {
    var ua = navigator.userAgent || '';
    return /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);
  }
  function homeScreen() {
    return navigator.standalone === true
      || !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
  }
  function webPush() {
    return 'serviceWorker' in navigator && 'PushManager' in window
      && 'Notification' in window && window.isSecureContext !== false;
  }

  // --- talking to the server -------------------------------------------

  var configured = null;
  function config() {
    if (!configured) {
      configured = fetch(CONFIG, { credentials: 'same-origin',
                                   headers: { Accept: 'application/json' } })
        .then(function (r) {
          if (!r.ok) { throw new Error('The server did not answer (' + r.status + ').'); }
          return r.json();
        })
        .catch(function (err) { configured = null; throw err; });
    }
    return configured;
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.error || ('The server said no (' + r.status + ').'));
          err.status = r.status;
          throw err;
        }
        return data;
      });
    });
  }

  function bytes(text) {
    var plain = (text + '===='.slice((text.length % 4) || 4))
      .replace(/-/g, '+').replace(/_/g, '/');
    var raw = window.atob(plain), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
    return out;
  }

  // Whether a subscription was made with the server's current key. One made
  // with an older key is refused by its push service, so it must be renewed.
  function sameKey(sub, key) {
    var had = sub && sub.options && sub.options.applicationServerKey;
    if (!had) { return true; }
    var a = new Uint8Array(had), b = bytes(key);
    if (a.length !== b.length) { return false; }
    for (var i = 0; i < a.length; i++) { if (a[i] !== b[i]) { return false; } }
    return true;
  }

  // --- in a browser ------------------------------------------------------

  function worker() {
    return navigator.serviceWorker.getRegistration('/').then(function (reg) {
      return reg || navigator.serviceWorker.register(WORKER, { scope: '/' });
    }).then(function () { return navigator.serviceWorker.ready; });
  }

  function currentSubscription() {
    return navigator.serviceWorker.getRegistration('/').then(function (reg) {
      return reg ? reg.pushManager.getSubscription() : null;
    });
  }

  function letGo(sub) {
    write(OWNER, '');
    return Promise.resolve(sub && sub.unsubscribe()).catch(function () {});
  }

  function webSync() {
    if (!webPush()) { return Promise.resolve(); }
    return currentSubscription().then(function (sub) {
      if (!sub) {
        if (read(OWNER) === me) { write(OWNER, ''); }
        return null;
      }
      if (read(OWNER) !== me) {
        // Turned on here by another account: its alerts stop coming here.
        return post(UNSUBSCRIBE, { kind: 'web', endpoint: sub.endpoint, release: true })
          .catch(function () {}).then(function () { return letGo(sub); });
      }
      if (!firstThisVisit(visit)) { return null; }
      return post(SUBSCRIBE, { kind: 'web', subscription: sub.toJSON(), check: true })
        .then(function (answer) {
          if (answer && answer.known === false) { return letGo(sub); }
          return null;
        }, function () { return null; });   // not asked: go by what is known here
    });
  }

  function webStatus() {
    if (!webPush()) {
      reason = appleTouch() && !homeScreen() ? SAID.homeScreen : SAID.noBrowser;
      return Promise.resolve('unsupported');
    }
    if (Notification.permission === 'denied') {
      reason = SAID.blockedWeb;
      return Promise.resolve('blocked');
    }
    return config().then(function (cfg) {
      if (!cfg.web || !cfg.web.enabled) {
        reason = (cfg.web && cfg.web.note) || SAID.serverOff;
        return 'unsupported';
      }
      return currentSubscription().then(function (sub) {
        if (!sub || Notification.permission !== 'granted' || read(OWNER) !== me) {
          return 'off';
        }
        if (!sameKey(sub, cfg.web.public_key)) { reason = SAID.stale; return 'off'; }
        return 'on';
      });
    });
  }

  function ask() {
    return new Promise(function (resolve) {
      // Older Safari answers through a callback rather than a promise.
      var asked = Notification.requestPermission(resolve);
      if (asked && asked.then) { asked.then(resolve); }
    });
  }

  function webEnable() {
    if (!webPush()) { return webStatus(); }
    // Asked first, inside the tap: Safari and Firefox only put the question
    // to someone who has just pressed something.
    var answer = Notification.permission === 'granted' ? Promise.resolve('granted') : ask();
    return answer.then(function (said) {
      if (said === 'denied') { reason = SAID.blockedWeb; return 'blocked'; }
      if (said !== 'granted') { reason = ''; return 'off'; }
      return Promise.all([config(), worker()]).then(function (got) {
        var cfg = got[0], reg = got[1];
        if (!cfg.web || !cfg.web.enabled) { throw new Error(SAID.serverOff); }
        var key = cfg.web.public_key;
        return reg.pushManager.getSubscription().then(function (old) {
          if (old && !sameKey(old, key)) {
            return old.unsubscribe().then(function () { return null; });
          }
          return old;
        }).then(function (sub) {
          return sub || reg.pushManager.subscribe({ userVisibleOnly: true,
                                                    applicationServerKey: bytes(key) });
        });
      }).then(function (sub) {
        return post(SUBSCRIBE, { kind: 'web', subscription: sub.toJSON() });
      }).then(function () {
        write(OWNER, me);
        firstThisVisit(visit);          // just told the server: no check needed
        reason = '';
        return 'on';
      });
    });
  }

  function webDisable() {
    if (!webPush()) { return Promise.resolve('off'); }
    return currentSubscription().then(function (sub) {
      write(OWNER, '');
      if (!sub) { return 'off'; }
      // Unsubscribed here even if the server could not be told: its push
      // service then calls the address gone, and the server forgets it.
      return post(UNSUBSCRIBE, { kind: 'web', endpoint: sub.endpoint })
        .catch(function () {})
        .then(function () { return sub.unsubscribe(); })
        .then(function () { return 'off'; });
    });
  }

  function webDevice() {
    return currentSubscription().then(function (sub) {
      return sub ? { kind: 'web', endpoint: sub.endpoint } : {};
    });
  }

  // --- in the iPhone app ---------------------------------------------------

  // register() answers through the 'registration' event, with the device's
  // APNs token in hex, or through 'registrationError'.
  function appToken() {
    var push = native();
    return new Promise(function (resolve, reject) {
      var done = false, handles = [];
      function finish(fn, value) {
        if (done) { return; }
        done = true;
        handles.forEach(function (h) {
          Promise.resolve(h).then(function (x) { if (x && x.remove) { x.remove(); } });
        });
        fn(value);
      }
      handles.push(push.addListener('registration', function (token) {
        finish(resolve, token && token.value);
      }));
      handles.push(push.addListener('registrationError', function (err) {
        finish(reject, new Error((err && err.error) || 'The iPhone would not register.'));
      }));
      window.setTimeout(function () {
        finish(reject, new Error('The iPhone did not answer. Try again in a moment.'));
      }, 20000);
      Promise.resolve(push.register()).catch(function (err) { finish(reject, err); });
    });
  }

  function appSync() {
    var push = native(), token = read(TOKEN);
    if (!push || !token) { return Promise.resolve(); }
    function forget() { write(OWNER, ''); write(TOKEN, ''); }
    if (read(OWNER) !== me) {
      // Turned on in the app by another account: its alerts stop coming here.
      return post(UNSUBSCRIBE, { kind: 'apns', token: token, release: true })
        .catch(function () {}).then(forget);
    }
    if (!firstThisVisit(visit)) { return Promise.resolve(); }
    return push.checkPermissions().then(function (p) {
      if (p.receive !== 'granted') {
        // Switched off in the iPhone's Settings: the server stops sending.
        return post(UNSUBSCRIBE, { kind: 'apns', token: token })
          .catch(function () {}).then(forget);
      }
      // Apple can change a device's token, so the current one is sent, in
      // place of the one the server has.
      return appToken().then(function (fresh) {
        return post(SUBSCRIBE, { kind: 'apns', token: fresh, label: 'iPhone app',
                                 check: true, replaces: token }).then(function (answer) {
          if (answer && answer.known === false) { forget(); }
          else { write(TOKEN, fresh); }
        });
      });
    }).catch(function () {});
  }

  function appStatus() {
    var push = native();
    if (!push) { reason = SAID.serverOff; return Promise.resolve('unsupported'); }
    return config().then(function (cfg) {
      if (!cfg.apns || !cfg.apns.enabled) { reason = SAID.serverOff; return 'unsupported'; }
      return push.checkPermissions().then(function (p) {
        if (p.receive === 'denied') { reason = SAID.blockedApp; return 'blocked'; }
        if (p.receive !== 'granted' || !read(TOKEN) || read(OWNER) !== me) { return 'off'; }
        return 'on';
      });
    });
  }

  function appEnable() {
    var push = native();
    if (!push) { return appStatus(); }
    return config().then(function (cfg) {
      if (!cfg.apns || !cfg.apns.enabled) { reason = SAID.serverOff; return 'unsupported'; }
      return push.requestPermissions().then(function (p) {
        if (p.receive === 'denied') { reason = SAID.blockedApp; return 'blocked'; }
        if (p.receive !== 'granted') { reason = ''; return 'off'; }
        return appToken().then(function (token) {
          return post(SUBSCRIBE, { kind: 'apns', token: token, label: 'iPhone app' })
            .then(function () {
              write(TOKEN, token);
              write(OWNER, me);
              firstThisVisit(visit);
              reason = '';
              return 'on';
            });
        });
      });
    });
  }

  function appDisable() {
    var token = read(TOKEN);
    write(OWNER, '');
    write(TOKEN, '');
    if (!token) { return Promise.resolve('off'); }
    // The token stays with the iPhone; only the server forgets it. Apple
    // asks apps not to unregister from notifications to switch them off.
    return post(UNSUBSCRIBE, { kind: 'apns', token: token })
      .catch(function () {}).then(function () { return 'off'; });
  }

  // --- the page's switch ------------------------------------------------------

  // Once a page: the device checked against the server before anything says
  // whether it is on.
  var synced = null;
  function sync() {
    if (!synced) {
      synced = (me ? (app() ? appSync() : webSync()) : Promise.resolve())
        .catch(function () {});
    }
    return synced;
  }

  var API = {
    supported: function () { return native() ? true : webPush(); },
    status: function () {
      reason = '';
      if (!me) { return Promise.resolve('unsupported'); }
      return sync().then(function () { return app() ? appStatus() : webStatus(); });
    },
    enable: function () {
      reason = '';
      if (!me) { return Promise.resolve('unsupported'); }
      return app() ? appEnable() : webEnable();
    },
    disable: function () {
      reason = '';
      return app() ? appDisable() : webDisable();
    },
    test: function () {
      var device = app()
        ? Promise.resolve(read(TOKEN) ? { kind: 'apns', token: read(TOKEN) } : {})
        : webDevice();
      return device.then(function (body) { return post(TEST, body); });
    },
    why: function () { return reason; },
    mine: function () { return !!me && read(OWNER) === me; }
  };
  window.OrenthNotify = API;

  // Inside the app, a tapped notification opens the page it is about.
  (function () {
    var push = native();
    if (!push) { return; }
    push.addListener('pushNotificationActionPerformed', function (action) {
      var data = action && action.notification && action.notification.data;
      var path = data && data.url;
      if (typeof path !== 'string' || path.charAt(0) !== '/' || path.charAt(1) === '/'
          || path.indexOf('\\') !== -1) {
        return;
      }
      // Read as a browser reads it, which drops tabs and line breaks, so a
      // path that only looks like one of ours cannot open another site.
      var target;
      try { target = new URL(path, window.location.origin); } catch (e) { return; }
      if (target.origin === window.location.origin) {
        window.location.assign(target.pathname + target.search + target.hash);
      }
    });
  })();

  // --- the cards on the Monitor and Settings pages -----------------------------

  var LABEL = { on: 'On', off: 'Off', blocked: 'Blocked', unsupported: 'Not available' };
  var PLAIN = {
    on: 'On for this device.',
    off: 'Off. Nothing is sent to this device.',
    blocked: '',
    unsupported: ''
  };

  function bind(card) {
    var chip = card.querySelector('[data-notify-state]');
    var said = card.querySelector('[data-notify-said]');
    var buttons = {
      on: card.querySelector('[data-notify-act="on"]'),
      off: card.querySelector('[data-notify-act="off"]'),
      test: card.querySelector('[data-notify-act="test"]')
    };
    var webWords = card.querySelector('[data-notify-web]');
    var appWords = card.querySelector('[data-notify-app]');
    if (app() && webWords && appWords) { webWords.hidden = true; appWords.hidden = false; }

    function show(answer) {
      card.hidden = false;
      chip.textContent = LABEL[answer] || answer;
      chip.className = 'chip ' + (answer === 'on' ? 'long' : answer === 'blocked' ? 'short' : 'mute');
      said.textContent = API.why() || PLAIN[answer] || '';
      buttons.on.hidden = answer !== 'off';
      buttons.off.hidden = answer !== 'on';
      buttons.test.hidden = answer !== 'on';
    }
    function failed(err) {
      said.textContent = 'That did not work: ' + ((err && err.message) || 'no reason given') + '.';
    }
    function act(button, run) {
      button.addEventListener('click', function () {
        button.disabled = true;
        run().catch(failed).then(function () { button.disabled = false; });
      });
    }

    act(buttons.on, function () { return API.enable().then(show); });
    act(buttons.off, function () { return API.disable().then(show); });
    act(buttons.test, function () {
      return API.test().then(function (out) {
        said.textContent = out.ok
          ? 'Sent. It should appear here within a few seconds.'
          : 'It could not be sent: ' + ((out.errors || [])[0] || 'no reason given') + '.';
      });
    });

    API.status().then(function (answer) {
      // A browser with nothing to offer and nothing useful to say shows no
      // card at all, rather than a switch that cannot work.
      if (answer === 'unsupported' && !API.why()) { return; }
      show(answer);
    }).catch(function () {});
  }

  function start() {
    if (!me) { return; }
    // Every signed-in page checks the device, card or no card, so a shared
    // phone lets the last person's notifications go as soon as it can.
    sync();
    var cards = document.querySelectorAll('[data-notify]');
    for (var i = 0; i < cards.length; i++) { bind(cards[i]); }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
