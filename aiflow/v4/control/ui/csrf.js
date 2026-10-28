/* Cross-site request protection: every state-changing request to this site carries the session's CSRF token
   (the aiflow4_csrf cookie, set at sign-in); the server rejects a console request without it. */
(function () {
  var nativeFetch = window.fetch;
  function csrfCookie() { var m = document.cookie.match(/(?:^|;\s*)aiflow4_csrf=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  window.fetch = function (input, init) {
    init = init || {};
    var method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var sameSite = url.charAt(0) === '/' && url.charAt(1) !== '/' || url.indexOf(location.origin + '/') === 0;
      var token = csrfCookie();
      if (sameSite && token) {
        var headers = new Headers(init.headers || (input && input.headers) || {});
        if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', token);
        init = Object.assign({}, init, { headers: headers });
      }
    }
    return nativeFetch.call(window, input, init);
  };
})();
