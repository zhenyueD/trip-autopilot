/* Google Maps/Places browser adapter. No model calls, persistent content cache, or route fabrication. */
(function (root) {
  'use strict';
  const MAX_PLACES = 8;
  const TIMEOUT_MS = 15000;
  const text = value => typeof value === 'string' ? value.trim().slice(0, 240) : '';
  const list = value => Array.isArray(value) ? value : [];
  const nameOf = entity => text(typeof entity === 'string' ? entity : entity?.name || entity?.title);
  const contextOf = entity => nameOf(entity?.destination || entity?.city)
    || list(entity?.destinations).map(nameOf).filter(Boolean).slice(0, 3).join(' ');
  const queryOf = entity => [nameOf(entity), contextOf(entity)].filter(Boolean).join(' ');
  const httpsURL = value => {
    try { const url = new URL(value); return url.protocol === 'https:' ? url.href : ''; } catch { return ''; }
  };

  function searchURL(entity) {
    const params = new URLSearchParams({ api: '1', query: queryOf(entity) || 'Google Maps' });
    return `https://www.google.com/maps/search/?${params}`;
  }

  function directionsURL(items, mode) {
    const names = list(items).map(queryOf).filter(Boolean);
    if (names.length < 2) return searchURL(names[0]);
    const params = new URLSearchParams({ api: '1', origin: names[0], destination: names.at(-1) });
    // Three waypoints are supported across mobile browsers; never truncate a longer day silently.
    if (names.length > 5) return null;
    if (names.length > 2) params.set('waypoints', names.slice(1, -1).join('|'));
    if (['driving', 'walking', 'bicycling', 'transit'].includes(mode)) params.set('travelmode', mode);
    return `https://www.google.com/maps/dir/?${params}`;
  }

  function coordinates(entity) {
    const lat = typeof entity?.lat === 'function' ? entity.lat() : entity?.lat;
    const lng = typeof entity?.lng === 'function' ? entity.lng() : entity?.lng;
    return Number.isFinite(lat) && Number.isFinite(lng) && Math.abs(lat) <= 90 && Math.abs(lng) <= 180 ? { lat, lng } : null;
  }

  function failure(code) {
    const messages = {
      timeout: 'Google 地图请求超时，请检查网络后重试。',
      authorization: 'Google 地图授权失败，请检查专用浏览器 Key、域名限制、Maps JavaScript API / Places API (New) 与计费状态。',
      reload: 'Google 地图配置已改变，请刷新页面后重试。',
      network: 'Google 地图加载失败，请检查网络或浏览器拦截后重试。',
      quota: 'Google Places 请求额度已达到限制，请稍后重试或检查配额。',
      places: 'Google Places 地点查询失败，请检查 Places API (New) 授权后重试。'
    };
    const error = new Error(messages[code] || messages.network); error.mapsCode = code; return error;
  }

  function publicError(error) {
    if (error?.mapsCode) return failure(error.mapsCode);
    const reason = String(error?.code || error?.message || '');
    if (/DENIED|AUTH|KEY|BILLING|ApiNotActivated|RefererNotAllowed/i.test(reason)) return failure('authorization');
    if (/QUOTA|OVER_QUERY_LIMIT|RESOURCE_EXHAUSTED/i.test(reason)) return failure('quota');
    return failure('places');
  }

  function withTimeout(promise, timeoutMs) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(failure('timeout')), timeoutMs);
      Promise.resolve(promise).then(resolve, reject).finally(() => clearTimeout(timer));
    });
  }

  // One script per browser client. Only the dedicated, explicitly supplied browser key is used.
  function createSDKLoader(windowObject, documentObject, onAuthFailure, timeoutMs = TIMEOUT_MS) {
    let pending; let activeKey = ''; let authorizationError; let sequence = 0;
    const previousAuthFailure = windowObject.gm_authFailure;
    return function loadSDK(config) {
      if (authorizationError) return Promise.reject(authorizationError);
      if (activeKey && activeKey !== config.browser_key) return Promise.reject(failure('reload'));
      if (pending) return pending;
      if (!documentObject || !config.browser_key) return Promise.reject(failure('network'));
      activeKey = config.browser_key;
      pending = new Promise((resolve, reject) => {
        const script = documentObject.createElement('script');
        const callback = `__travelGoogleMapsLoaded${++sequence}`;
        let settled = false;
        const cleanup = () => { clearTimeout(timer); windowObject[callback] = () => {}; };
        const fail = error => {
          if (settled) return;
          settled = true; cleanup(); script.remove(); reject(error);
        };
        const timer = setTimeout(() => fail(failure('timeout')), timeoutMs);
        windowObject.gm_authFailure = () => {
          authorizationError = failure('authorization'); fail(authorizationError);
          onAuthFailure?.(authorizationError);
          if (typeof previousAuthFailure === 'function') previousAuthFailure();
        };
        windowObject[callback] = () => {
          if (settled) return;
          if (!windowObject.google?.maps?.importLibrary) { fail(failure('network')); return; }
          settled = true; cleanup(); resolve(windowObject.google.maps);
        };
        const params = new URLSearchParams({ key: config.browser_key, v: 'weekly', loading: 'async', callback,
          language: 'zh-CN', auth_referrer_policy: 'origin' });
        script.src = `https://maps.googleapis.com/maps/api/js?${params}`;
        script.async = true;
        script.referrerPolicy = 'strict-origin-when-cross-origin';
        script.nonce = documentObject.querySelector('script[nonce]')?.nonce || '';
        script.onerror = () => fail(failure('network'));
        documentObject.head.append(script);
      });
      pending.catch(() => { pending = null; });
      return pending;
    };
  }

  function createClient(options = {}) {
    const timeoutMs = options.timeoutMs || TIMEOUT_MS;
    let config = {}; let generation = 0; let lastRender; let map; let mapElement; let sdk;
    let markers = []; let lastPoints = []; let loadedSDK; let mapReady; let cancelMapReady; let fatalError;
    let queried = new Set(); const searches = new Map(); const photos = new Map();
    const queue = []; let active = 0;
    const loadSDK = options.loadSDK || createSDKLoader(options.window || root, options.document || root.document,
      error => {
        fatalError = error; generation++; pump();
        emit(lastRender, generation, 'error', error.message, lastPoints.length);
      }, timeoutMs);
    const configured = () => config.configured === true && Boolean(text(config.browser_key));

    function emit(request, revision, status, message, located = 0) {
      if (revision !== generation) return;
      request?.onState?.({ status, message, located, total: list(request.items).length });
    }

    function configure(next = {}) {
      const previous = JSON.stringify(config);
      if (loadedSDK && config.browser_key !== text(next.browser_key)) fatalError = failure('reload');
      config = { configured: next.configured === true, browser_key: text(next.browser_key),
        map_id: text(next.map_id), places_enabled: next.places_enabled === true };
      if (JSON.stringify(config) !== previous) reset();
      return { configured: configured(), places_enabled: config.places_enabled };
    }

    async function ensureSDK() {
      if (fatalError) throw fatalError;
      if (!loadedSDK) loadedSDK = withTimeout(Promise.resolve().then(() => loadSDK(config)), timeoutMs)
        .then(value => { sdk = value; return value; }).catch(error => { loadedSDK = null; throw publicError(error); });
      return loadedSDK;
    }

    async function library(name) {
      const maps = await ensureSDK();
      return withTimeout(maps.importLibrary(name), timeoutMs);
    }

    function pump() {
      while (active < 2 && queue.length) {
        const job = queue.shift();
        if (job.expired || job.revision !== generation) { job.resolve(null); continue; }
        active++;
        Promise.resolve().then(job.run).then(job.resolve, job.reject).finally(() => { active--; pump(); });
      }
    }

    function limited(run, revision) {
      return new Promise((resolve, reject) => {
        const job = { run, revision, expired: false,
          resolve: value => { clearTimeout(timer); resolve(value); },
          reject: error => { clearTimeout(timer); reject(error); } };
        // Google Places requests cannot be aborted. Timed-out requests retain their concurrency
        // slot until the actual SDK promise settles, so a retry cannot create a request storm.
        const timer = setTimeout(() => { job.expired = true; reject(failure('timeout')); }, timeoutMs);
        queue.push(job); pump();
      });
    }

    function admit(query) {
      if (!query || (!queried.has(query) && queried.size >= MAX_PLACES)) return false;
      queried.add(query); return true;
    }

    function findPlace(entity, revision) {
      const query = queryOf(entity); const key = `${revision}:${query}`;
      if (searches.has(key)) return searches.get(key);
      if (!admit(query)) return Promise.resolve(null);
      const promise = limited(async () => {
        const { Place } = await library('places');
        if (revision !== generation) return null;
        const result = await Place.searchByText({ textQuery: query,
          fields: ['id', 'displayName', 'location', 'googleMapsURI'], maxResultCount: 1 });
        return revision === generation ? result?.places?.[0] || null : null;
      }, revision);
      searches.set(key, promise);
      promise.finally(() => searches.delete(key)).catch(() => {});
      return promise;
    }

    function rowsFor(request) {
      const supplies = request.supplies instanceof Map ? request.supplies
        : new Map(list(request.supplies).map(supply => [supply.id, supply]));
      const seen = new Set(); const rows = [];
      for (const item of list(request.items)) {
        const supply = supplies.get(item.supply_id); const name = nameOf(supply) || nameOf(item);
        const position = coordinates(supply) || coordinates(item);
        const entity = { name, destination: supply?.destination || item.destination || item.city,
          destinations: request.destinations };
        const identity = item.supply_id || queryOf(entity) || JSON.stringify(position);
        if ((!name && !position) || seen.has(identity)) continue;
        seen.add(identity); rows.push({ entity, position, searched: false, order: rows.length + 1 });
      }
      return rows.slice(0, MAX_PLACES);
    }

    function clearMarkers() {
      markers.forEach(marker => { marker.map = null; }); markers = []; lastPoints = [];
    }

    function waitForMap(instance) {
      return new Promise((resolve, reject) => {
        let listener;
        const finish = error => { clearTimeout(timer); listener?.remove(); cancelMapReady = null; error ? reject(error) : resolve(); };
        const timer = setTimeout(() => finish(failure('timeout')), timeoutMs);
        cancelMapReady = () => finish();
        listener = instance.addListener('tilesloaded', () => finish());
      });
    }

    function fit(points, Bounds) {
      if (!map || !points.length) return;
      if (points.length === 1) { map.setCenter(points[0].position); map.setZoom(14); return; }
      const bounds = new Bounds(); points.forEach(point => bounds.extend(point.position)); map.fitBounds(bounds, 44);
    }

    async function draw(request, points, revision) {
      const [{ Map: GoogleMap }, { AdvancedMarkerElement }, { LatLngBounds }] = await Promise.all([
        library('maps'), library('marker'), library('core')]);
      if (revision !== generation) return;
      if (!map || mapElement !== request.element) {
        cancelMapReady?.();
        map = new GoogleMap(request.element, { center: points[0].position, zoom: 13,
          mapId: config.map_id || 'DEMO_MAP_ID', mapTypeControl: false, streetViewControl: false });
        mapElement = request.element;
        const createdMap = map;
        mapReady = waitForMap(map).catch(error => {
          if (map === createdMap) { clearMarkers(); map = null; mapElement = null; }
          throw error;
        });
      }
      clearMarkers();
      points.forEach(point => {
        markers.push(new AdvancedMarkerElement({ map, position: point.position,
          title: `${point.order}. ${nameOf(point.entity)} · ${point.searched ? 'Google 搜索定位，非精确入口' : '来源坐标，入口请现场核对'}` }));
      });
      lastPoints = points; fit(points, LatLngBounds);
      await mapReady;
      if (fatalError) throw fatalError;
    }

    async function resolveRows(rows, revision) {
      const errors = [];
      const points = await Promise.all(rows.map(async row => {
        if (row.position) return row;
        if (!config.places_enabled) return null;
        try {
          const place = await findPlace(row.entity, revision); const position = coordinates(place?.location);
          return position ? { ...row, position, searched: true } : null;
        } catch (error) { errors.push(publicError(error)); return null; }
      }));
      return { points: points.filter(Boolean), errors };
    }

    async function render(request = {}) {
      const revision = ++generation; lastRender = request; queried = new Set(); clearMarkers(); pump();
      if (!configured()) {
        emit(request, revision, 'unconfigured', '尚未配置专用 Google Maps 浏览器 Key；仍可通过 Google Maps 链接查看地点。'); return;
      }
      if (!request.element || !list(request.items).length) {
        emit(request, revision, 'empty', '当天暂无可显示的地点。'); return;
      }
      emit(request, revision, 'loading', '正在加载 Google 地图与当天地点…');
      try {
        await ensureSDK(); if (revision !== generation) return;
        const { points, errors } = await resolveRows(rowsFor(request), revision);
        if (revision !== generation) return;
        if (!points.length) {
          emit(request, revision, errors.length ? 'error' : 'empty', errors[0]?.message
            || (config.places_enabled ? '未找到可靠的地点坐标，请用 Google Maps 链接核对。' : '地点缺少来源坐标；启用 Places API (New) 后可查询定位。')); return;
        }
        await draw(request, points, revision);
        const note = `已定位 ${points.length}/${list(request.items).length} 处地点。`
          + (points.some(point => point.searched) ? '搜索定位非精确入口，请在 Google Maps 中核对。' : '来源坐标供定位参考，入口请核对。')
          + (list(request.items).length > MAX_PLACES ? `当前最多显示 ${MAX_PLACES} 处地点。` : '')
          + '导航请在 Google Maps 中确认，图上不绘制未经核验的路线。';
        emit(request, revision, errors.length ? 'error' : 'ready', errors[0] ? `${note} ${errors[0].message}` : note, points.length);
      } catch (error) { emit(request, revision, 'error', publicError(error).message, lastPoints.length); }
    }

    async function photoFor(entity, revision) {
      const id = text(entity?.place_id || entity?.google_place_id);
      if (id && !admit(`id:${id}`)) return null;
      const place = id ? { id } : await findPlace(entity, revision);
      if (!place?.id || revision !== generation) return null;
      return limited(async () => {
        const { Place } = await library('places');
        if (revision !== generation) return null;
        // A new Place object and fresh photo field request for every completed lookup.
        const fresh = new Place({ id: place.id });
        await fresh.fetchFields({ fields: ['photos', 'displayName', 'googleMapsURI'] });
        if (revision !== generation || !fresh.photos?.length) return null;
        const photo = fresh.photos[0]; const url = httpsURL(photo.getURI({ maxWidth: 1000, maxHeight: 700 }));
        if (!url) return null;
        return { url, displayName: fresh.displayName || nameOf(entity), source: 'google_places', cacheable: false,
          googleMapsURI: httpsURL(photo.googleMapsURI || fresh.googleMapsURI), flagContentURI: httpsURL(photo.flagContentURI),
          authorAttributions: list(photo.authorAttributions).map(author => ({ displayName: String(author.displayName || ''),
            uri: httpsURL(author.uri), photoURI: httpsURL(author.photoURI) })) };
      }, revision);
    }

    function findPhoto(entity) {
      if (!configured() || !config.places_enabled) return Promise.resolve(null);
      const revision = generation; const key = `${revision}:${text(entity?.place_id || entity?.google_place_id) || queryOf(entity)}`;
      if (photos.has(key)) return photos.get(key);
      const promise = photoFor(entity, revision).catch(error => { throw publicError(error); });
      photos.set(key, promise);
      promise.finally(() => photos.delete(key)).catch(() => {});
      return promise;
    }

    function reset() {
      generation++; clearMarkers(); cancelMapReady?.(); map = null; mapElement = null; mapReady = null;
      lastRender = null; queried = new Set(); pump();
    }

    async function resize() {
      const revision = generation;
      if (!map || !lastPoints.length) return;
      sdk?.event?.trigger(map, 'resize');
      try { const { LatLngBounds } = await library('core'); if (revision === generation) fit(lastPoints, LatLngBounds); } catch { /* Render owns visible errors. */ }
    }

    function retry() { return lastRender ? render(lastRender) : Promise.resolve(); }
    return { configure, render, resize, reset, retry, findPhoto, searchURL, directionsURL };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { createClient, createSDKLoader, searchURL, directionsURL };
  if (root?.document) root.TravelGoogleMaps = createClient();
})(typeof window !== 'undefined' ? window : globalThis);
