(function (global) {
  'use strict';

  const API_URL = 'https://data.sncf.com/api/records/1.0/search/';
  const RECORDS_EXPORT_URL = 'https://data.sncf.com/api/explore/v2.1/catalog/datasets/tgvmax/exports/json';
  const TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
  const MAX_DAYS_AHEAD = 31;
  const INITIAL_STATIONS = [
    'PARIS (intramuros)', 'LYON (intramuros)', 'MARSEILLE ST CHARLES',
    'BORDEAUX ST JEAN', 'LILLE (intramuros)', 'STRASBOURG', 'NANTES',
    'RENNES', 'TOULOUSE MATABIAU', 'MONTPELLIER SAINT ROCH', 'NICE VILLE',
    'AVIGNON TGV', 'DIJON VILLE', 'AIX EN PROVENCE TGV', 'LA ROCHELLE VILLE'
  ];

  const CITY_CENTERS = {
    paris: [48.8566, 2.3522],
    lyon: [45.764, 4.8357],
    marseille: [43.3027, 5.3806],
    bordeaux: [44.8259, -0.5567],
    lille: [50.6366, 3.0704],
    strasbourg: [48.585, 7.7346],
    nantes: [47.2173, -1.5419],
    rennes: [48.1035, -1.6722],
    toulouse: [43.6112, 1.4536],
    montpellier: [43.6045, 3.8808],
    nice: [43.7047, 7.2619],
    grenoble: [45.1914, 5.7147],
    dijon: [47.323, 5.0272],
    avignon: [43.9218, 4.786],
    angers: [47.4646, -0.5568],
    'le mans': [47.9956, 0.192],
    tours: [47.3898, 0.693],
    brest: [48.388, -4.478],
    quimper: [47.9945, -4.092],
    bayonne: [43.4968, -1.4701],
    perpignan: [42.696, 2.8797],
    bruxelles: [50.8357, 4.3369],
    luxembourg: [49.5996, 6.1349],
    freiburg: [47.9978, 7.8426],
    frankfurt: [50.1071, 8.6638],
    karlsruhe: [48.9935, 8.4005]
  };

  function normalizeText(value) {
    return String(value || '')
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .replace(/[’'`´]/g, ' ')
      .replace(/[^a-zA-Z0-9]+/g, ' ')
      .replace(/\bsaint\b/g, 'st')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase();
  }

  function normalizeRecord(raw) {
    const fields = raw && raw.fields ? raw.fields : (raw || {});
    return {
      date: fields.date || '',
      origin: fields.origine || fields.origin || '',
      destination: fields.destination || '',
      departure: String(fields.heure_depart || fields.departure_time || '').slice(0, 5),
      arrival: String(fields.heure_arrivee || fields.arrival_time || '').slice(0, 5),
      trainNumber: String(fields.train_no || fields.num_train || fields.train_number || ''),
      reservable: normalizeText(fields.od_happy_card) === 'oui',
      updatedAt: raw && raw.record_timestamp ? raw.record_timestamp : (fields.record_timestamp || '')
    };
  }

  function uniqueRecords(records) {
    const seen = new Set();
    return records.filter(function (record) {
      const key = [record.date, record.origin, record.destination, record.departure, record.arrival, record.trainNumber].join('|');
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function stationScore(query, candidate) {
    const q = normalizeText(query);
    const c = normalizeText(candidate);
    if (!q || !c) return Number.POSITIVE_INFINITY;
    if (q === c) return 0;
    if (c === q + ' intramuros') return 1;

    const firstQueryWord = q.split(' ')[0];
    const firstCandidateWord = c.split(' ')[0];
    if (firstQueryWord === firstCandidateWord && c.includes('intramuros')) return 2;
    if (c.startsWith(q + ' ')) return 3 + Math.abs(c.length - q.length) / 100;
    if (q.startsWith(c + ' ')) return 4 + Math.abs(c.length - q.length) / 100;

    const tokens = q.split(' ').filter(function (token) { return token.length > 1; });
    if (tokens.length && tokens.every(function (token) { return c.includes(token); })) {
      return 5 + Math.abs(c.length - q.length) / 100;
    }
    return Number.POSITIVE_INFINITY;
  }

  function selectStation(query, stations) {
    let best = null;
    let bestScore = Number.POSITIVE_INFINITY;
    stations.forEach(function (station) {
      const score = stationScore(query, station);
      if (score < bestScore) {
        best = station;
        bestScore = score;
      }
    });
    return Number.isFinite(bestScore) ? best : null;
  }

  function groupByCounterpart(records, mode) {
    const groups = new Map();
    records.forEach(function (record) {
      const key = mode === 'to' ? record.origin : record.destination;
      if (!key) return;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(record);
    });
    groups.forEach(function (trains) {
      trains.sort(function (a, b) { return a.departure.localeCompare(b.departure); });
    });
    return Array.from(groups.entries())
      .map(function (entry) { return { station: entry[0], trains: entry[1] }; })
      .sort(function (a, b) {
        const firstA = a.trains[0] ? a.trains[0].departure : '99:99';
        const firstB = b.trains[0] ? b.trains[0].departure : '99:99';
        return firstA.localeCompare(firstB) || a.station.localeCompare(b.station, 'fr');
      });
  }

  function parseTime(value) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(value || '');
    if (!match) return null;
    return Number(match[1]) * 60 + Number(match[2]);
  }

  function minutesBetween(start, end) {
    const startMinutes = parseTime(start);
    let endMinutes = parseTime(end);
    if (startMinutes === null || endMinutes === null) return null;
    if (endMinutes < startMinutes) endMinutes += 24 * 60;
    return endMinutes - startMinutes;
  }

  function formatDuration(minutes) {
    if (minutes === null || !Number.isFinite(minutes) || minutes < 0) return '';
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (!hours) return rest + ' min';
    return hours + ' h ' + String(rest).padStart(2, '0');
  }

  const core = {
    normalizeText: normalizeText,
    normalizeRecord: normalizeRecord,
    uniqueRecords: uniqueRecords,
    stationScore: stationScore,
    selectStation: selectStation,
    groupByCounterpart: groupByCounterpart,
    minutesBetween: minutesBetween,
    formatDuration: formatDuration
  };

  global.TrainquilleCore = core;
  if (typeof module !== 'undefined' && module.exports) module.exports = core;
  if (typeof document === 'undefined') return;

  const state = {
    mode: 'from',
    cache: new Map(),
    suggestions: new Set(INITIAL_STATIONS),
    coordinates: new Map(),
    coordinateCache: new Map(),
    coordinatesReady: Promise.resolve(),
    map: null,
    mapLayer: null,
    mapMarkers: new Map(),
    lastSearch: null
  };

  const elements = {};

  document.addEventListener('DOMContentLoaded', init);

  function init() {
    [
      'search-form', 'station', 'station-list', 'station-label', 'date', 'search-button',
      'search-button-label', 'result-status', 'loading-state', 'error-state', 'error-title',
      'error-message', 'retry-button', 'results-view', 'results-kicker', 'results-title',
      'results-meta', 'results-list', 'map-title', 'map-note'
    ].forEach(function (id) {
      elements[id] = document.getElementById(id);
    });

    configureDates();
    bindEvents();
    updateMode('from', false);
    populateSuggestions();
    initMap();
    state.coordinatesReady = loadCoordinates();
    loadRemoteSuggestions();
  }

  function bindEvents() {
    document.querySelectorAll('.mode-button').forEach(function (button) {
      button.addEventListener('click', function () { updateMode(button.dataset.mode, true); });
    });
    elements['search-form'].addEventListener('submit', runSearch);
    elements['retry-button'].addEventListener('click', function () {
      if (state.lastSearch) executeSearch(state.lastSearch.station, state.lastSearch.date);
    });
  }

  function localISODate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return year + '-' + month + '-' + day;
  }

  function configureDates() {
    const today = new Date();
    const maxDate = new Date(today);
    maxDate.setDate(maxDate.getDate() + MAX_DAYS_AHEAD);
    elements.date.min = localISODate(today);
    elements.date.max = localISODate(maxDate);
    elements.date.value = localISODate(today);
  }

  function updateMode(mode, resetResults) {
    state.mode = mode === 'to' ? 'to' : 'from';
    document.querySelectorAll('.mode-button').forEach(function (button) {
      const active = button.dataset.mode === state.mode;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });

    if (state.mode === 'to') {
      elements['station-label'].textContent = "Gare d'arrivée";
      elements.station.placeholder = 'Ex. Bordeaux, Marseille, Strasbourg';
      elements['search-button-label'].textContent = 'Voir les villes de départ';
    } else {
      elements['station-label'].textContent = 'Gare de départ';
      elements.station.placeholder = 'Ex. Paris, Lyon Part-Dieu, Nantes';
      elements['search-button-label'].textContent = 'Voir les destinations';
    }

    if (resetResults) showIntro();
  }

  function populateSuggestions() {
    const fragment = document.createDocumentFragment();
    Array.from(state.suggestions)
      .sort(function (a, b) { return displayStation(a).localeCompare(displayStation(b), 'fr'); })
      .forEach(function (station) {
        const option = document.createElement('option');
        option.value = displayStation(station);
        fragment.appendChild(option);
      });
    elements['station-list'].replaceChildren(fragment);
  }

  async function fetchJSON(url, timeoutMs) {
    const controller = new AbortController();
    const timeout = setTimeout(function () { controller.abort(); }, timeoutMs || 45000);
    try {
      const response = await fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error('Réponse SNCF ' + response.status);
      return await response.json();
    } finally {
      clearTimeout(timeout);
    }
  }

  async function loadRemoteSuggestions() {
    const params = new URLSearchParams();
    params.set('dataset', 'tgvmax');
    params.set('rows', '0');
    params.append('facet', 'origine');
    params.append('facet', 'destination');
    try {
      const data = await fetchJSON(API_URL + '?' + params.toString(), 20000);
      (data.facet_groups || []).forEach(function (group) {
        (group.facets || []).forEach(function (facet) {
          if (facet.name) state.suggestions.add(facet.name);
        });
      });
      populateSuggestions();
    } catch (error) {
      console.warn('[Trainquille] Suggestions SNCF indisponibles:', error);
    }
  }

  async function loadCoordinates() {
    try {
      const data = await fetchJSON('gares.json', 25000);
      (data || []).forEach(function (station) {
        const lat = Number(station.y_wgs84);
        const lng = Number(station.x_wgs84);
        if (!station.libelle || !Number.isFinite(lat) || !Number.isFinite(lng)) return;
        const key = normalizeText(station.libelle);
        if (!state.coordinates.has(key)) state.coordinates.set(key, [lat, lng]);
      });
    } catch (error) {
      console.warn('[Trainquille] Coordonnées locales indisponibles:', error);
    }
  }

  function initMap() {
    if (!global.L) {
      elements.map.textContent = 'Carte indisponible. Les résultats resteront consultables dans la liste.';
      elements.map.classList.add('map-unavailable');
      return;
    }
    state.map = global.L.map('map', { zoomControl: true, preferCanvas: true }).setView([46.6, 2.3], 5.25);
    global.L.tileLayer(TILE_URL, {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      maxZoom: 19
    }).addTo(state.map);
    state.mapLayer = global.L.layerGroup().addTo(state.map);
    setTimeout(function () { state.map.invalidateSize(); }, 80);
  }

  async function runSearch(event) {
    event.preventDefault();
    const station = elements.station.value.trim();
    const date = elements.date.value;
    if (!station || !date) {
      showError('Recherche incomplète', 'Choisissez une gare et une date avant de lancer la recherche.');
      return;
    }
    state.lastSearch = { station: station, date: date };
    await executeSearch(station, date);
  }

  async function executeSearch(stationQuery, date) {
    setBusy(true);
    showLoading();
    try {
      const records = await fetchRecordsForDate(date);
      const available = records.filter(function (record) { return record.reservable; });
      const stationField = state.mode === 'to' ? 'destination' : 'origin';
      const stations = Array.from(new Set(available.map(function (record) { return record[stationField]; }).filter(Boolean)));
      const selectedStation = selectStation(stationQuery, stations);

      if (!selectedStation) {
        const suggestions = closestStations(stationQuery, stations).slice(0, 3).map(displayStation);
        const hint = suggestions.length ? ' Essayez : ' + suggestions.join(', ') + '.' : '';
        showError('Gare introuvable ce jour-là', "Aucun départ TGV Max ne correspond à « " + stationQuery + " »." + hint);
        return;
      }

      elements.station.value = displayStation(selectedStation);
      const selectedKey = normalizeText(selectedStation);
      let matches = available.filter(function (record) {
        return normalizeText(record[stationField]) === selectedKey;
      });

      let pastDeparturesHidden = false;
      if (date === localISODate(new Date())) {
        const now = new Date();
        const nowMinutes = now.getHours() * 60 + now.getMinutes();
        const currentMatches = matches.filter(function (record) {
          const departure = parseTime(record.departure);
          return departure === null || departure >= nowMinutes;
        });
        pastDeparturesHidden = currentMatches.length !== matches.length;
        matches = currentMatches;
      }

      if (!matches.length) {
        showError(
          'Aucune place réservable',
          'La gare existe dans les données, mais aucune place TGV Max à venir n’est ouverte pour cette date.'
        );
        clearMap();
        return;
      }

      const groups = groupByCounterpart(matches, state.mode);
      renderResults(selectedStation, date, groups, matches.length, records, pastDeparturesHidden);
      await renderMap(selectedStation, groups);
    } catch (error) {
      console.error('[Trainquille] Recherche impossible:', error);
      const timedOut = error && error.name === 'AbortError';
      showError(
        timedOut ? 'La SNCF met trop de temps à répondre' : 'Impossible de charger les trains',
        timedOut
          ? 'La requête a dépassé 45 secondes. Vérifiez votre connexion puis réessayez.'
          : 'Le service Open Data SNCF est momentanément indisponible. Aucun résultat incomplet n’a été affiché.'
      );
    } finally {
      setBusy(false);
    }
  }

  async function fetchRecordsForDate(date) {
    if (state.cache.has(date)) return state.cache.get(date);
    const params = new URLSearchParams();
    params.set('where', "date = date'" + date + "'");
    const data = await fetchJSON(RECORDS_EXPORT_URL + '?' + params.toString(), 45000);
    const rawRecords = Array.isArray(data) ? data : (data.records || []);
    const records = uniqueRecords(rawRecords.map(normalizeRecord));
    if (!records.length && rawRecords.length > 0) {
      throw new Error('La réponse SNCF ne contient aucun trajet exploitable.');
    }
    records.forEach(function (record) {
      if (record.origin) state.suggestions.add(record.origin);
      if (record.destination) state.suggestions.add(record.destination);
    });
    populateSuggestions();
    state.cache.set(date, records);
    return records;
  }

  function closestStations(query, stations) {
    const normalizedQuery = normalizeText(query);
    return stations
      .map(function (station) {
        const normalized = normalizeText(station);
        let score = stationScore(query, station);
        if (!Number.isFinite(score)) {
          const queryFirst = normalizedQuery.split(' ')[0] || '';
          score = normalized.includes(queryFirst) ? 20 + Math.abs(normalized.length - normalizedQuery.length) : 1000;
        }
        return { station: station, score: score };
      })
      .sort(function (a, b) { return a.score - b.score; })
      .map(function (item) { return item.station; });
  }

  function displayStation(value) {
    const lower = String(value || '').trim().toLocaleLowerCase('fr-FR');
    return lower
      .replace(/(^|[\s(\-])([a-zà-öø-ÿ])/g, function (_, prefix, letter) {
        return prefix + letter.toLocaleUpperCase('fr-FR');
      })
      .replace(/\bTgv\b/g, 'TGV')
      .replace(/\bCdg\b/g, 'CDG')
      .replace(/\bHbf\b/g, 'Hbf');
  }

  function formatDate(date) {
    return new Intl.DateTimeFormat('fr-FR', {
      weekday: 'long', day: 'numeric', month: 'long'
    }).format(new Date(date + 'T12:00:00'));
  }

  function formatUpdatedAt(records) {
    const timestamp = records.find(function (record) { return record.updatedAt; });
    if (!timestamp) return '';
    const date = new Date(timestamp.updatedAt);
    if (Number.isNaN(date.getTime())) return '';
    return 'Données actualisées le ' + new Intl.DateTimeFormat('fr-FR', {
      day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'
    }).format(date);
  }

  function renderResults(selectedStation, date, groups, trainCount, allRecords, pastDeparturesHidden) {
    elements['results-list'].replaceChildren();
    elements['results-kicker'].textContent = state.mode === 'to' ? 'Toutes les origines' : 'Toutes les destinations';
    elements['results-title'].textContent = displayStation(selectedStation);
    const groupLabel = state.mode === 'to' ? 'ville' + (groups.length > 1 ? 's' : '') + ' de départ' : 'destination' + (groups.length > 1 ? 's' : '');
    const updated = formatUpdatedAt(allRecords);
    const hiddenNote = pastDeparturesHidden ? ' · départs passés masqués' : '';
    elements['results-meta'].textContent = groups.length + ' ' + groupLabel + ' · ' + trainCount + ' train' + (trainCount > 1 ? 's' : '') + ' réservable' + (trainCount > 1 ? 's' : '') + ' · ' + formatDate(date) + (updated ? ' · ' + updated : '') + hiddenNote;

    const fragment = document.createDocumentFragment();
    groups.forEach(function (group, index) {
      fragment.appendChild(buildDestinationCard(group, index));
    });
    elements['results-list'].appendChild(fragment);
    showOnly('results-view');
    elements['results-view'].hidden = false;
  }

  function buildDestinationCard(group, index) {
    const article = createElement('article', 'destination-card');
    const heading = createElement('button', 'destination-head');
    heading.type = 'button';
    heading.title = 'Centrer ' + displayStation(group.station) + ' sur la carte';

    const nameWrap = createElement('span');
    nameWrap.appendChild(createElement('span', 'destination-index', String(index + 1).padStart(2, '0')));
    nameWrap.appendChild(createElement('span', 'destination-name', displayStation(group.station)));
    heading.appendChild(nameWrap);
    heading.appendChild(createElement('span', 'train-count', group.trains.length + ' train' + (group.trains.length > 1 ? 's' : '')));
    heading.addEventListener('click', function () { focusStationOnMap(group.station); });
    article.appendChild(heading);

    const list = createElement('div', 'train-list');
    const extraRows = [];
    group.trains.forEach(function (train, trainIndex) {
      const row = buildTrainRow(train);
      if (trainIndex >= 3) {
        row.hidden = true;
        extraRows.push(row);
      }
      list.appendChild(row);
    });
    article.appendChild(list);

    if (extraRows.length) {
      const toggle = createElement('button', 'expand-trains', 'Voir ' + extraRows.length + ' horaire' + (extraRows.length > 1 ? 's' : '') + ' de plus');
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', 'false');
      toggle.addEventListener('click', function () {
        const expanded = toggle.getAttribute('aria-expanded') === 'true';
        extraRows.forEach(function (row) { row.hidden = expanded; });
        toggle.setAttribute('aria-expanded', String(!expanded));
        toggle.textContent = expanded ? 'Voir ' + extraRows.length + ' horaire' + (extraRows.length > 1 ? 's' : '') + ' de plus' : 'Réduire les horaires';
      });
      article.appendChild(toggle);
    }
    return article;
  }

  function buildTrainRow(train) {
    const row = createElement('div', 'train-row');
    row.appendChild(createElement('span', 'train-time', train.departure || '—'));

    const line = createElement('span', 'train-line');
    const duration = formatDuration(minutesBetween(train.departure, train.arrival));
    line.appendChild(createElement('span', '', duration || 'TGV'));
    row.appendChild(line);
    row.appendChild(createElement('span', 'train-time', train.arrival || '—'));

    const meta = createElement('span', 'train-meta');
    meta.appendChild(createElement('span', 'max-chip', 'MAX'));
    meta.appendChild(document.createTextNode(train.trainNumber ? 'Train ' + train.trainNumber : 'TGV'));
    row.appendChild(meta);
    return row;
  }

  function createElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function setBusy(busy) {
    elements['search-button'].disabled = busy;
    elements['search-form'].setAttribute('aria-busy', String(busy));
  }

  function showOnly(id) {
    ['result-status', 'loading-state', 'error-state', 'results-view'].forEach(function (candidate) {
      elements[candidate].hidden = candidate !== id;
    });
  }

  function showIntro() {
    showOnly('result-status');
  }

  function showLoading() {
    showOnly('loading-state');
  }

  function showError(title, message) {
    elements['error-title'].textContent = title;
    elements['error-message'].textContent = message;
    showOnly('error-state');
  }

  function clearMap() {
    if (state.mapLayer) state.mapLayer.clearLayers();
    state.mapMarkers.clear();
    if (state.map) state.map.setView([46.6, 2.3], 5.25);
    elements['map-title'].textContent = 'France entière';
    elements['map-note'].textContent = 'Aucun trajet à placer sur la carte pour cette recherche.';
  }

  function lookupCoordinates(station) {
    const key = normalizeText(station);
    if (state.coordinateCache.has(key)) return state.coordinateCache.get(key);
    if (state.coordinates.has(key)) {
      const exact = state.coordinates.get(key);
      state.coordinateCache.set(key, exact);
      return exact;
    }

    const cityKey = Object.keys(CITY_CENTERS).find(function (city) {
      return key === city || key.startsWith(city + ' ') || key.startsWith(city + ' intramuros');
    });
    if (cityKey) {
      state.coordinateCache.set(key, CITY_CENTERS[cityKey]);
      return CITY_CENTERS[cityKey];
    }

    let best = null;
    let bestScore = Number.POSITIVE_INFINITY;
    state.coordinates.forEach(function (coordinates, candidate) {
      let score = Number.POSITIVE_INFINITY;
      if (candidate.startsWith(key + ' ')) score = candidate.includes('tgv') || candidate.includes('ville') ? 1 : 3;
      else if (key.startsWith(candidate + ' ')) score = 4;
      else if (candidate.includes(key) && key.length >= 5) score = 8 + Math.abs(candidate.length - key.length) / 100;
      if (score < bestScore) {
        best = coordinates;
        bestScore = score;
      }
    });
    state.coordinateCache.set(key, best);
    return best;
  }

  async function renderMap(selectedStation, groups) {
    if (!state.map || !state.mapLayer) return;
    await state.coordinatesReady;
    state.mapLayer.clearLayers();
    state.mapMarkers.clear();

    const anchor = lookupCoordinates(selectedStation);
    const bounds = global.L.latLngBounds([]);
    if (anchor) {
      addMapMarker(selectedStation, anchor, true, groups.reduce(function (total, group) { return total + group.trains.length; }, 0));
      bounds.extend(anchor);
    }

    let mapped = 0;
    groups.forEach(function (group) {
      const coordinates = lookupCoordinates(group.station);
      if (!coordinates) return;
      mapped += 1;
      addMapMarker(group.station, coordinates, false, group.trains.length);
      bounds.extend(coordinates);
      if (anchor) {
        global.L.polyline([anchor, coordinates], {
          color: '#0f7b82', weight: 1.7, opacity: 0.28, interactive: false
        }).addTo(state.mapLayer);
      }
    });

    elements['map-title'].textContent = displayStation(selectedStation);
    elements['map-note'].textContent = mapped + ' gare' + (mapped > 1 ? 's' : '') + ' placée' + (mapped > 1 ? 's' : '') + ' sur la carte · cliquez sur une destination pour la centrer.';
    if (bounds.isValid()) state.map.fitBounds(bounds, { padding: [35, 35], maxZoom: 7.5 });
    setTimeout(function () { state.map.invalidateSize(); }, 80);
  }

  function addMapMarker(station, coordinates, isAnchor, trainCount) {
    const marker = global.L.circleMarker(coordinates, {
      radius: isAnchor ? 7 : 5,
      color: '#ffffff',
      weight: 2,
      fillColor: isAnchor ? '#f2a51a' : '#0f7b82',
      fillOpacity: 1
    }).addTo(state.mapLayer);
    const popup = createElement('div', 'map-popup');
    popup.appendChild(createElement('b', '', displayStation(station)));
    popup.appendChild(createElement('span', '', trainCount + ' train' + (trainCount > 1 ? 's' : '') + ' TGV Max'));
    marker.bindPopup(popup);
    state.mapMarkers.set(normalizeText(station), marker);
  }

  function focusStationOnMap(station) {
    const marker = state.mapMarkers.get(normalizeText(station));
    if (!marker || !state.map) return;
    state.map.setView(marker.getLatLng(), Math.max(state.map.getZoom(), 8), { animate: true });
    marker.openPopup();
    if (window.matchMedia('(max-width: 980px)').matches) {
      document.querySelector('.map-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);
