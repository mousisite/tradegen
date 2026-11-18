/* Orenth Pro, bought inside the iPhone app through Apple.
 *
 * The server sends this only to pages drawn in the app, for someone signed
 * in, while it sells Pro in the app; and it does nothing unless the app has
 * its AppPurchase plugin (mobile/ios/App/App/AppPurchasePlugin.swift).
 *
 * On every page, because the app forgets every listener when a page loads:
 *   - it listens for the transactions StoreKit reports by itself (renewals,
 *     Ask to Buy approvals, purchases on another device, and ones left over
 *     from last time), and
 *   - it asks StoreKit for any transaction not yet finished,
 * and sends each to the server, which checks Apple's signature and decides
 * who has Pro. A transaction is finished only once the server has answered
 * with anything but "invalid", so a request that is lost costs nothing:
 * StoreKit offers the transaction again.
 *
 * On the Pricing page it is the subscribe screen: each plan's name, length
 * and price exactly as the App Store gives them, Subscribe, Restore
 * Purchases, and moving Pro here from another Orenth account on the same
 * Apple Account. Anywhere, a [data-iap-manage] link opens Apple's own
 * Manage Subscriptions sheet.
 *
 * Plugin calls go through Capacitor.nativePromise; there is no bundler.
 */
(function () {
  'use strict';

  var C = window.Capacitor;
  if (!C || typeof C.isNativePlatform !== 'function' || !C.isNativePlatform()) {
    return;
  }

  var PLUGIN = 'AppPurchase';
  var SUBSCRIPTIONS = 'https://apps.apple.com/account/subscriptions';
  // Transactions the server called invalid, so each page load this session
  // does not send them again. StoreKit still holds them for a later launch.
  var INVALID_KEY = 'orenth-iap-invalid';

  function noop() {}

  function failure(code, message) {
    var e = new Error(message || code);
    e.code = code;
    return e;
  }

  function has(name) {
    if (typeof C.isPluginAvailable === 'function') { return C.isPluginAvailable(name); }
    return !!(C.Plugins && C.Plugins[name]);
  }

  var ready = has(PLUGIN) && (typeof C.getPlatform !== 'function' || C.getPlatform() === 'ios');

  function ap(method, options) {
    if (typeof C.nativePromise !== 'function') {
      return Promise.reject(failure('UNAVAILABLE', 'The App Store is not available.'));
    }
    try {
      return C.nativePromise(PLUGIN, method, options || {});
    } catch (e) {
      return Promise.reject(e);
    }
  }

  function listen(eventName, callback) {
    try {
      if (typeof C.addListener === 'function') { return C.addListener(PLUGIN, eventName, callback); }
      var plugin = C.Plugins && C.Plugins[PLUGIN];
      if (plugin && typeof plugin.addListener === 'function') { return plugin.addListener(eventName, callback); }
    } catch (e) { /* nothing to listen with */ }
    return null;
  }

  // ---------------------------------------------------------------------------
  // The server.
  // ---------------------------------------------------------------------------
  function answer(r) {
    return r.json().catch(function () { return {}; }).then(function (body) {
      if (!r.ok) {
        var e = failure('SERVER', body.error || ('The server answered ' + r.status + '.'));
        e.status = r.status;
        throw e;
      }
      return body;
    });
  }

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(answer);
  }

  function getState() {
    return fetch('/api/apple/state', { credentials: 'same-origin', cache: 'no-store' }).then(answer);
  }

  function invalidList() {
    try { return JSON.parse(window.sessionStorage.getItem(INVALID_KEY) || '[]'); } catch (e) { return []; }
  }

  function markInvalid(id) {
    try {
      var list = invalidList();
      if (list.indexOf(id) < 0) { list.push(id); }
      window.sessionStorage.setItem(INVALID_KEY, JSON.stringify(list.slice(-50)));
    } catch (e) { /* private mode: it is just sent again */ }
  }

  var inflight = {};
  var watchers = [];

  function tell(out) {
    for (var i = 0; i < watchers.length; i++) {
      try { watchers[i](out); } catch (e) { /* one watcher's problem */ }
    }
  }

  // One StoreKit transaction to the server, then finished once the server has
  // it. Resolves with the server's answer.
  function deliver(t) {
    if (!t || typeof t.jws !== 'string' || !t.jws) { return Promise.resolve(null); }
    var id = String(t.transactionId || '');
    if (id && inflight[id]) { return inflight[id]; }
    var sent = post('/api/apple/transactions', { jws: [t.jws] }).then(function (out) {
      var result = (out.results && out.results[0]) || {};
      if (result.outcome === 'invalid') {
        if (id) { markInvalid(id); }
        tell(out);
        return out;
      }
      var finished = id ? ap('finish', { transactionId: id }).catch(noop) : Promise.resolve();
      return finished.then(function () { tell(out); return out; });
    });
    if (id) {
      inflight[id] = sent;
      sent.then(function () { delete inflight[id]; }, function () { delete inflight[id]; });
    }
    return sent;
  }

  function quietly(t) {
    if (!t || invalidList().indexOf(String(t.transactionId || '')) >= 0) { return; }
    deliver(t).catch(noop);
  }

  if (ready) {
    // Renewals, approvals and purchases elsewhere; the plugin holds any that
    // came while no page was listening and hands them over now.
    listen('transaction', quietly);
    // Anything an earlier page left unfinished (closed mid-request, offline).
    ap('unfinished').then(function (r) {
      ((r && r.transactions) || []).forEach(quietly);
    }).catch(noop);
  }

  // ---------------------------------------------------------------------------
  // Manage subscription: Apple's own sheet, or Apple's page if that fails.
  // Caught before native.js, which would open the link in a browser sheet.
  // ---------------------------------------------------------------------------
  function openApplePage() {
    if (has('Browser')) {
      C.nativePromise('Browser', 'open', { url: SUBSCRIPTIONS }).catch(function () {
        location.assign(SUBSCRIPTIONS);
      });
    } else {
      location.assign(SUBSCRIPTIONS);
    }
  }

  document.addEventListener('click', function (e) {
    var start = e.target;
    var link = start && start.closest ? start.closest('[data-iap-manage]') : null;
    if (!link || !ready) { return; }
    e.preventDefault();
    e.stopPropagation();
    ap('manageSubscriptions').catch(openApplePage);
  }, true);

  // ---------------------------------------------------------------------------
  // The subscribe screen, on the Pricing page.
  // ---------------------------------------------------------------------------
  var box = document.querySelector('[data-app-purchase]');
  if (!box) { return; }
  var shop = box.querySelector('[data-iap-shop]');
  var said = box.querySelector('[data-iap-said]');
  var restoreButton = box.querySelector('[data-iap-restore]');
  var products = shop ? (shop.getAttribute('data-products') || '').split(',').filter(Boolean) : [];
  var busy = false;
  // Set once the server says this Apple Account's Pro belongs to another
  // Orenth account. From then on no plan offers Subscribe: the App Store
  // would only change that account's subscription, and this one would get
  // nothing for it. Moving Pro here is offered instead.
  var elsewhere = false;
  var MOVE_INSTEAD = 'This Apple Account already pays for Pro on another Orenth account. ' +
    'Move it to this account instead.';

  function say(text) {
    if (said) { said.textContent = text || ''; }
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text) { node.textContent = text; }
    return node;
  }

  function buttons() {
    return box.querySelectorAll('button');
  }

  function hold(on) {
    busy = on;
    var all = buttons();
    for (var i = 0; i < all.length; i++) { all[i].disabled = on; }
  }

  function again(delay) {
    window.setTimeout(function () { location.reload(); }, delay || 1200);
  }

  if (!ready) {
    // An older app, from before Pro was sold in it.
    if (shop) {
      var list = shop.querySelector('[data-iap-products]');
      list.textContent = '';
      list.appendChild(el('p', 'prose', 'Subscribing needs the newest version of the Orenth app. ' +
        'Update it from the App Store, then come back here.'));
    }
    if (restoreButton) { restoreButton.hidden = true; }
    return;
  }

  var UNITS = { day: ['day', 'days'], week: ['week', 'weeks'], month: ['month', 'months'], year: ['year', 'years'] };
  var LENGTHS = { day: 'Daily', week: 'Weekly', month: 'Monthly', year: 'Yearly' };

  function span(unit, value) {
    var names = UNITS[unit];
    var n = Number(value) || 1;
    if (!names) { return ''; }
    return n === 1 ? names[0] : n + ' ' + names[1];
  }

  function lengthOf(p) {
    var n = Number(p.periodValue) || 1;
    if (n === 1 && LENGTHS[p.periodUnit]) { return LENGTHS[p.periodUnit] + ' subscription'; }
    return 'Subscription for ' + span(p.periodUnit, n) + ' at a time';
  }

  function introOf(p) {
    var o = p.introOffer;
    if (!o || !p.introEligible) { return ''; }
    var every = span(p.periodUnit, p.periodValue);
    var first = span(o.periodUnit, (Number(o.periodValue) || 1) * (Number(o.periodCount) || 1));
    if (o.paymentMode === 'freeTrial') {
      return 'Free for the first ' + first + ', then ' + p.displayPrice + ' per ' + every + '.';
    }
    if (o.paymentMode === 'payUpFront') {
      return o.displayPrice + ' for the first ' + first + ', then ' + p.displayPrice + ' per ' + every + '.';
    }
    if (o.paymentMode === 'payAsYouGo') {
      return o.displayPrice + ' per ' + span(o.periodUnit, o.periodValue) + ' for the first ' + first +
        ', then ' + p.displayPrice + ' per ' + every + '.';
    }
    return '';
  }

  // One plan, as the App Store describes it. Its price is the largest text.
  function card(p) {
    var every = span(p.periodUnit, p.periodValue);
    var item = el('div', 'iapitem');
    item.appendChild(el('div', 'iapname', p.title || 'Orenth Pro'));
    item.appendChild(el('div', 'iaplength', lengthOf(p)));
    var price = el('div', 'iapprice');
    price.appendChild(el('span', 'iapamount', p.displayPrice));
    if (every) {
      price.appendChild(document.createTextNode(' '));
      price.appendChild(el('span', 'iapper', 'per ' + every));
    }
    item.appendChild(price);
    var intro = introOf(p);
    if (intro) { item.appendChild(el('p', 'fine', intro)); }
    var buy = el('button', 'btn', 'Subscribe');
    buy.type = 'button';
    buy.hidden = elsewhere;
    buy.setAttribute('data-buy', p.id);
    buy.setAttribute('aria-label', 'Subscribe to ' + (p.title || 'Orenth Pro') + ', ' +
      p.displayPrice + (every ? ' per ' + every : ''));
    item.appendChild(buy);
    return item;
  }

  function showPlans() {
    var list = shop.querySelector('[data-iap-products]');
    return ap('products', { ids: products }).then(function (r) {
      var found = {};
      ((r && r.products) || []).forEach(function (p) {
        if (p && p.id && typeof p.displayPrice === 'string' && p.displayPrice) { found[p.id] = p; }
      });
      var shown = products.filter(function (id) { return found[id]; });
      list.textContent = '';
      if (!shown.length) { throw failure('NO_PRODUCTS', 'No plans.'); }
      shown.forEach(function (id) { list.appendChild(card(found[id])); });
    }).catch(function () {
      list.textContent = '';
      list.appendChild(el('p', 'prose', 'The App Store did not show the plans just now.'));
      var retry = el('button', 'btn quiet', 'Try again');
      retry.type = 'button';
      retry.addEventListener('click', function () {
        list.textContent = '';
        list.appendChild(el('p', 'soft', 'Asking the App Store for the plans…'));
        showPlans();
      });
      list.appendChild(retry);
    });
  }

  // This Apple Account's Pro belongs to another Orenth account: offer the
  // move, and no purchase, now or for plans drawn later.
  function showElsewhere() {
    elsewhere = true;
    if (!shop) { return; }
    var notice = shop.querySelector('[data-iap-elsewhere]');
    if (notice) { notice.hidden = false; }
    var buys = shop.querySelectorAll('[data-buy]');
    for (var i = 0; i < buys.length; i++) { buys[i].hidden = true; }
  }

  function outcomes(out) {
    return ((out && out.results) || []).map(function (r) { return r.outcome; });
  }

  // What this Apple Account already holds, sent to the server without asking
  // for a password. The server decides whose it is: this account's (then
  // Pro is restored here), or another Orenth account's. Resolves with its
  // answer, or null when the Apple Account holds nothing.
  function checkHeld() {
    return ap('entitlements').then(function (r) {
      var jws = (r && r.jws) || [];
      if (!jws.length) { return null; }
      return post('/api/apple/transactions', { jws: jws.slice(0, 20) });
    });
  }

  function afterServer(out, done) {
    var seen = outcomes(out);
    if (out && out.pro) {
      say(out.source === 'apple' ? (done || 'You have Pro. Thank you.')
        : 'Your account already has Pro.');
      again();
      return;
    }
    if (seen.indexOf('other_account') >= 0) {
      showElsewhere();
      say('');
      return;
    }
    if (seen.indexOf('invalid') >= 0) {
      say('Apple took the payment, but Orenth could not confirm it with Apple just now. ' +
        'It is tried again by itself the next time a page opens.');
      return;
    }
    if (seen.indexOf('applied') >= 0 || seen.indexOf('stale') >= 0) {
      say('Apple confirmed it, but it does not give this account Pro now. ' +
        'An ended or test subscription does not.');
      return;
    }
    say('No Orenth Pro subscription was found for this Apple Account.');
  }

  function buy(productId) {
    if (busy) { return; }
    if (elsewhere) {
      showElsewhere();
      say(MOVE_INSTEAD);
      return;
    }
    hold(true);
    say('Opening the App Store…');
    var paid = false;
    getState().then(function (state) {
      if (!state.enabled) { throw failure('OFF', 'Subscribing is not available just now.'); }
      if (state.pro) {
        // Pro already, through Apple or on the website: never sell it twice.
        say(state.source === 'apple' ? 'You already have Pro through Apple.'
          : 'Your account already has Pro, so there is nothing to buy.');
        again(1500);
        return null;
      }
      // Checked again just before buying: the check made when the page
      // opened may not have answered yet.
      return checkHeld().then(function (held) {
        if (held && held.pro) {
          say('You already have Pro through Apple.');
          again(1500);
          return null;
        }
        if (outcomes(held).indexOf('other_account') >= 0) {
          showElsewhere();
          say(MOVE_INSTEAD);
          return null;
        }
        return ap('purchase', { productId: productId, appAccountToken: state.appAccountToken })
          .then(function (t) {
            paid = true;
            say('Confirming with Apple…');
            return deliver(t);
          })
          .then(function (out) { if (out) { afterServer(out); } });
      });
    }).catch(function (err) {
      var code = err && err.code;
      if (code === 'CANCELLED') { say(''); }
      else if (code === 'PENDING') {
        say('The purchase is waiting for approval. Pro starts as soon as it is approved.');
      } else if (code === 'BUSY') { say('A purchase is already open.'); }
      else if (code === 'NOT_FOUND') { say('That plan is not available just now.'); }
      else if (code === 'OFF') { say(err.message); }
      else if (paid) {
        say('Apple took the payment, but Orenth could not record it just now. It finishes by ' +
          'itself the next time a page opens in the app.');
      } else if (code === 'SERVER') {
        say('Orenth could not be reached, so nothing was bought. Try again in a moment.');
      } else {
        say('The purchase did not go through, and nothing was charged.');
      }
    }).then(function () { hold(false); });
  }

  function restore() {
    if (busy) { return; }
    hold(true);
    say('Asking the App Store…');
    // Asks for the Apple Account's password, so only ever from this tap.
    ap('restore').then(function (r) {
      return post('/api/apple/transactions', { jws: (r && r.jws) || [] });
    }).then(function (out) {
      afterServer(out, 'Restored. You have Pro.');
    }).catch(function (err) {
      if (err && err.code === 'SERVER') {
        say('Orenth could not be reached just now. Try again in a moment.');
      } else if (err && err.code === 'CANCELLED') {
        say('');
      } else {
        say('The App Store could not restore purchases just now. Try again in a moment.');
      }
    }).then(function () { hold(false); });
  }

  function moveHere() {
    if (busy) { return; }
    hold(true);
    say('Moving Pro to this account…');
    ap('entitlements').then(function (r) {
      var mine = ((r && r.transactions) || []).filter(function (t) {
        return t && typeof t.jws === 'string' && products.indexOf(t.productId) >= 0 && !t.revoked;
      });
      if (!mine.length) { throw failure('NONE', 'none'); }
      return post('/api/apple/move', { jws: mine[0].jws });
    }).then(function (out) {
      if (out.outcome === 'moved' || out.outcome === 'already_here') {
        say('Pro is on this account now.');
        again();
      } else if (out.outcome === 'too_soon') {
        var when = out.after ? new Date(out.after * 1000).toLocaleDateString() : 'next month';
        say('This subscription moved between accounts less than 30 days ago. It can move again on ' +
          when + '.');
      } else {
        say('This Apple Account has no running Orenth Pro subscription to move.');
      }
    }).catch(function (err) {
      if (err && err.code === 'NONE') {
        say('This Apple Account has no running Orenth Pro subscription to move.');
      } else {
        say('It could not be moved just now. Try again in a moment.');
      }
    }).then(function () { hold(false); });
  }

  box.addEventListener('click', function (e) {
    var start = e.target;
    var button = start && start.closest ? start.closest('button') : null;
    if (!button || button.disabled) { return; }
    if (button.hasAttribute('data-buy')) { buy(button.getAttribute('data-buy')); }
    else if (button.hasAttribute('data-iap-restore')) { restore(); }
    else if (button.hasAttribute('data-iap-move')) { moveHere(); }
  });

  if (shop && products.length) {
    showPlans();
    // A purchase approved, renewed or made on another device while this page
    // is open: show it.
    watchers.push(function (out) { if (out && out.pro) { again(); } });
    // Quietly, without asking for a password: what this Apple Account
    // already holds. It may be this account's (restored), or another's.
    checkHeld().then(function (out) {
      if (!out) { return; }
      if (out.pro) { again(); return; }
      if (outcomes(out).indexOf('other_account') >= 0) { showElsewhere(); }
    }).catch(noop);
  }
})();
