'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const integer = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 });
  const decimal = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });
  const clockFormat = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3 });
  const shortClockFormat = new Intl.DateTimeFormat('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const fullDateFormat = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'medium', timeStyle: 'long' });
  const seen = new Set();
  const pendingComparisons = new Set();
  let initialized = false;
  let fetchInProgress = false;
  let state = null;
  let soundEnabled = false;
  let notificationsEnabled = false;
  let audioContext = null;
  let lastFeedSignature = '';

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function number(value) {
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  }

  function date(value) {
    if (!value) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }

  function time(value, precise = false) {
    const parsed = date(value);
    return parsed ? (precise ? clockFormat : shortClockFormat).format(parsed) : '—';
  }

  function duration(value, signed = false) {
    const numeric = number(value);
    if (numeric === null) return '—';
    const sign = signed && numeric > 0 ? '+' : '';
    return `${sign}${decimal.format(numeric)} s`;
  }

  function offerKey(offer) {
    return `${String(offer.id)}|${String(offer.event_id ?? offer.detected_at)}|${String(offer.price)}`;
  }

  function safeOfferUrl(value) {
    try {
      const url = new URL(value);
      if (url.protocol === 'https:' && (url.hostname === 'warframe.market' || url.hostname === 'www.warframe.market')) return url.href;
    } catch (_) { /* An invalid API URL must never become a clickable link. */ }
    return null;
  }

  function showError(message) {
    $('error-banner').textContent = message;
    $('error-banner').hidden = false;
  }

  function alertStatus(extra = '') {
    const options = [];
    options.push(soundEnabled ? 'Son activé' : 'Son désactivé');
    options.push(notificationsEnabled ? 'notifications activées' : 'notifications désactivées');
    $('alert-message').textContent = `${options.join(' · ')}. ${extra || 'Seules les nouvelles détections déclenchent une alerte.'}`;
  }

  function playSound() {
    if (!audioContext || !soundEnabled) return;
    void audioContext.resume().then(() => {
      const start = audioContext.currentTime;
      [740, 990].forEach((frequency, index) => {
        const oscillator = audioContext.createOscillator();
        const gain = audioContext.createGain();
        const at = start + index * 0.13;
        oscillator.type = 'sine';
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0, at);
        gain.gain.linearRampToValueAtTime(0.12, at + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.001, at + 0.2);
        oscillator.connect(gain);
        gain.connect(audioContext.destination);
        oscillator.start(at);
        oscillator.stop(at + 0.21);
      });
    }).catch(() => alertStatus('Le navigateur a suspendu le son. Désactivez puis réactivez-le.'));
  }

  function notifyOffers(offers, isDemo) {
    if (!offers.length) return;
    if (soundEnabled) playSound();
    if (!notificationsEnabled || !('Notification' in window) || Notification.permission !== 'granted') return;
    const offer = offers[0];
    const title = `${isDemo ? '[DÉMO] ' : ''}${offers.length > 1 ? `${offers.length} offres détectées` : 'Dual Toxocyst détecté'}`;
    try {
      const notification = new Notification(title, {
        body: `${integer.format(number(offer.price) ?? 0)} pl · ${offer.seller || 'Vendeur inconnu'}${offers.length > 1 ? ` · et ${offers.length - 1} autre(s)` : ''}`,
        tag: `riven-${offerKey(offer)}`,
      });
      notification.onclick = () => { window.focus(); notification.close(); };
    } catch (_) {
      notificationsEnabled = false;
      updateNotificationButton();
      alertStatus('Ce navigateur ne permet pas les notifications dans ce contexte.');
    }
  }

  function updateNotificationButton() {
    const button = $('notification-button');
    button.setAttribute('aria-pressed', String(notificationsEnabled));
    $('notification-label').textContent = notificationsEnabled ? 'Notifications actives' : 'Notifications';
  }

  $('sound-button').addEventListener('click', async () => {
    if (!soundEnabled) {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) { alertStatus('Le son n’est pas disponible dans ce navigateur.'); return; }
      try {
        audioContext = audioContext || new AudioContextClass();
        await audioContext.resume();
      } catch (_) { alertStatus('Le navigateur n’a pas pu activer le son.'); return; }
    }
    soundEnabled = !soundEnabled;
    $('sound-button').setAttribute('aria-pressed', String(soundEnabled));
    $('sound-label').textContent = soundEnabled ? 'Son activé' : 'Activer le son';
    alertStatus(soundEnabled ? 'Un signal test confirme l’activation du son.' : 'Seules les nouvelles détections déclenchent une alerte.');
    if (soundEnabled) playSound();
  });

  $('notification-button').addEventListener('click', async () => {
    if (notificationsEnabled) { notificationsEnabled = false; updateNotificationButton(); alertStatus(); return; }
    if (!('Notification' in window)) { alertStatus('Les notifications ne sont pas disponibles dans ce navigateur.'); return; }
    try {
      const permission = await Notification.requestPermission();
      notificationsEnabled = permission === 'granted';
      updateNotificationButton();
      alertStatus(permission === 'denied' ? 'Notifications refusées : leur autorisation se règle dans les paramètres du navigateur.' : 'Seules les nouvelles détections déclenchent une alerte.');
    } catch (_) { alertStatus('Les notifications ne sont pas disponibles dans ce contexte.'); }
  });

  async function compare(offer, button) {
    if (pendingComparisons.has(offer.id)) return;
    const receivedAt = new Date().toISOString();
    pendingComparisons.add(offer.id);
    button.disabled = true;
    button.textContent = 'Enregistrement…';
    try {
      const response = await fetch('/api/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: offer.id, event_id: offer.event_id, received_at: receivedAt }),
        signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error(`Enregistrement impossible (HTTP ${response.status}).`);
      const next = await response.json();
      pendingComparisons.delete(offer.id);
      render(next);
      $('error-banner').hidden = true;
    } catch (error) {
      showError(error.name === 'TimeoutError' ? 'Le serveur ne répond pas. La comparaison n’a pas été enregistrée.' : `Comparaison : ${error.message}`);
      pendingComparisons.delete(offer.id);
      lastFeedSignature = '';
      if (state) renderOffers(state.offers || [], new Set());
    }
  }

  $('aleca-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const receivedAt = new Date().toISOString();
    const button = $('aleca-submit');
    if (button.disabled) return;
    const id = $('aleca-offer-id').value.trim();
    const price = Number($('aleca-offer-price').value);
    const message = $('aleca-form-message');
    if (!id || !Number.isSafeInteger(price)) {
      message.textContent = 'Indiquez l’URL officielle ou l’identifiant et un prix entier.';
      message.classList.add('form-error');
      return;
    }
    button.disabled = true;
    message.classList.remove('form-error');
    message.textContent = 'Enregistrement de l’arrivée…';
    try {
      const response = await fetch('/api/aleca', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, price, received_at: receivedAt }),
        signal: AbortSignal.timeout(10000),
      });
      const next = await response.json();
      if (!response.ok) throw new Error(typeof next.error === 'string' ? next.error : `HTTP ${response.status}`);
      render(next);
      message.textContent = `Arrivée Aleca notée à ${time(receivedAt, true)}. Le rapprochement utilise l’identifiant et le prix exacts.`;
      $('aleca-form').reset();
    } catch (error) {
      message.classList.add('form-error');
      message.textContent = `Arrivée non enregistrée : ${error.name === 'TimeoutError' ? 'le serveur ne répond pas.' : error.message}`;
    } finally {
      button.disabled = false;
    }
  });

  function buildRow(offer, fresh) {
    const row = element('tr', fresh ? 'fresh' : '');
    const offerCell = element('td');
    const title = element('span', 'offer-title', offer.name || 'Dual Toxocyst');
    title.title = String(offer.name || 'Dual Toxocyst');
    offerCell.append(title);
    const seller = element('span', 'seller-line');
    const sellerStatus = ['online', 'ingame', 'invisible'].includes(offer.seller_status) ? offer.seller_status : 'offline';
    const sellerDot = element('span', `seller-dot ${sellerStatus}`);
    sellerDot.title = { online: 'En ligne', ingame: 'En jeu', invisible: 'Statut inconnu', offline: 'Hors ligne' }[sellerStatus];
    seller.append(sellerDot, element('span', 'seller-name', offer.seller || 'Vendeur inconnu'));
    offerCell.append(seller);
    const kind = ['new', 'price_change', 'baseline'].includes(offer.kind) ? offer.kind : 'baseline';
    offerCell.append(element('span', `kind-tag ${kind}`, { new: 'NOUVELLE OFFRE', price_change: 'PRIX MODIFIÉ', baseline: 'ÉTAT INITIAL' }[kind]));

    const priceCell = element('td');
    const price = element('div', 'offer-price', number(offer.price) === null ? '—' : integer.format(offer.price));
    price.append(element('span', '', 'pl'));
    priceCell.append(price);

    const detectedCell = element('td');
    const detected = element('span', 'detected-time', time(offer.detected_at, true));
    const detectedDate = date(offer.detected_at);
    if (detectedDate) detected.title = fullDateFormat.format(detectedDate);
    detectedCell.append(detected);
    const createdDetail = element('span', 'time-detail', `Création ${time(offer.created_at)} · âge API ${duration(offer.api_age_seconds)}`);
    const createdDate = date(offer.created_at);
    if (createdDate) createdDetail.title = `Création déclarée : ${fullDateFormat.format(createdDate)}. Cet âge ne mesure pas la latence de publication.`;
    detectedCell.append(createdDetail);

    const compareCell = element('td');
    const lead = number(offer.lead_seconds);
    if (offer.aleca_seen_at && lead !== null) {
      compareCell.append(element('span', `comparison-result${lead < 0 ? ' negative' : ''}`, `${duration(lead, true)} ${lead >= 0 ? 'd’avance' : 'de retard'}`));
      compareCell.append(element('span', 'comparison-detail', `Détection → clic ${time(offer.aleca_seen_at)} · approx.`));
    } else if (kind === 'baseline') {
      compareCell.append(element('span', 'baseline-label', 'Présente au démarrage'));
    } else {
      const button = element('button', 'compare-button', pendingComparisons.has(offer.id) ? 'Enregistrement…' : 'Aleca reçue');
      button.type = 'button';
      button.setAttribute('aria-label', `Aleca reçue pour l’offre ${offer.event_id ?? offer.id} de ${offer.seller || 'ce vendeur'}`);
      button.disabled = pendingComparisons.has(offer.id);
      button.title = 'Cliquer dès réception de l’alerte AlecaFrame correspondant à cette offre';
      button.addEventListener('click', () => { void compare(offer, button); });
      compareCell.append(button);
      compareCell.append(element('span', 'comparison-detail', 'Cliquez à l’arrivée de l’alerte'));
    }

    const linkCell = element('td');
    const url = safeOfferUrl(offer.url);
    if (url) {
      const link = element('a', 'open-link', '↗');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.setAttribute('aria-label', `Voir l’offre de ${offer.seller || 'ce vendeur'} sur warframe.market`);
      link.title = 'Ouvrir sur warframe.market';
      linkCell.append(link);
    }
    row.append(offerCell, priceCell, detectedCell, compareCell, linkCell);
    return row;
  }

  function renderOffers(offers, newKeys) {
    const signature = JSON.stringify(offers);
    if (signature === lastFeedSignature) return;
    const body = $('offers-body');
    const active = document.activeElement;
    const focusedLabel = active && body.contains(active) ? active.getAttribute('aria-label') : null;
    const fragment = document.createDocumentFragment();
    offers.forEach((offer) => fragment.append(buildRow(offer, newKeys.has(offerKey(offer)))));
    body.replaceChildren(fragment);
    lastFeedSignature = signature;
    if (focusedLabel) {
      const replacement = [...body.querySelectorAll('[aria-label]')].find((node) => node.getAttribute('aria-label') === focusedLabel);
      if (replacement) replacement.focus({ preventScroll: true });
    }
  }

  function render(next) {
    if (!next || typeof next !== 'object' || !Array.isArray(next.offers)) throw new Error('Réponse du serveur invalide.');
    state = next;
    const config = next.config || {};
    const status = next.status || {};
    const comparison = next.comparison || {};
    const isDemo = next.mode === 'demo';
    const offers = next.offers;
    const newOffers = [];
    const newKeys = new Set();
    for (const offer of offers) {
      const key = offerKey(offer);
      if (initialized && !seen.has(key) && (offer.kind === 'new' || offer.kind === 'price_change')) { newOffers.push(offer); newKeys.add(key); }
      seen.add(key);
    }
    initialized = true;
    const apiConnected = status.seeded && !['blocked', 'error', 'backoff'].includes(status.phase);
    $('mode-badge').textContent = isDemo ? 'DÉMO SIMULÉE' : apiConnected ? 'API CONNECTÉE' : 'API NON CONNECTÉE';
    $('mode-badge').className = `mode-badge ${isDemo ? 'demo' : apiConnected ? 'live' : ''}`;
    $('demo-banner').hidden = !isDemo;
    const phase = status.phase || 'starting';
    $('phase-label').textContent = { starting: 'Connexion à l’API', polling: 'Requête API en cours', watching: isDemo ? 'Simulation en cours' : 'Surveillance active', backoff: 'Collecte en échec · nouvelle tentative prévue', blocked: 'Accès à l’API bloqué', error: 'Surveillance interrompue' }[phase] || 'État de la surveillance';
    $('status-dot').className = `status-dot ${phase === 'watching' || (phase === 'polling' && status.seeded) ? 'active' : (phase === 'error' || phase === 'blocked') ? 'error' : 'warning'}`;
    $('phase-message').textContent = status.message || 'En attente de nouvelles offres.';
    $('last-success').textContent = time(status.last_success_at);
    $('coverage-warning').textContent = status.coverage_warning || 'Instantané global récent : la couverture dépend du cache et de la taille du flux. Une consultation chaque seconde ne garantit pas une détection en une seconde.';
    const cacheAge = number(status.api_cache_age_seconds);
    const cacheControl = typeof status.api_cache_control === 'string' ? status.api_cache_control : '';
    $('cache-info').hidden = cacheAge === null && !cacheControl;
    $('cache-info').textContent = `${cacheAge === null ? '' : `Âge du cache API : ${duration(cacheAge)}`}${cacheAge !== null && cacheControl ? ' · ' : ''}${cacheControl}`;
    $('weapon-label').textContent = config.label || 'Dual Toxocyst';
    $('min-price').textContent = integer.format(number(config.min_price) ?? 10);
    $('max-price').textContent = integer.format(number(config.max_price) ?? 2000);
    $('aleca-offer-price').min = String(number(config.min_price) ?? 10);
    $('aleca-offer-price').max = String(number(config.max_price) ?? 2000);
    const nextAttempt = date(status.next_poll_at);
    const clockNow = date(next.clock) || new Date();
    const secondsUntilRetry = nextAttempt ? Math.max(0, Math.ceil((nextAttempt - clockNow) / 1000)) : null;
    const failedCollection = ['backoff', 'blocked', 'error'].includes(phase);
    $('api-error-banner').hidden = !failedCollection;
    if (failedCollection) {
      $('api-error-banner').textContent = `${status.seeded ? 'La collecte ne reçoit plus de données.' : 'Surveillance non opérationnelle : aucune réponse API valide reçue.'} ${status.message || ''}${phase === 'backoff' && secondsUntilRetry !== null ? ` Prochaine tentative dans ${integer.format(secondsUntilRetry)} s.` : ' Collecte arrêtée.'}`;
    }
    $('interval').textContent = phase === 'backoff' ? 'En pause' : ['blocked', 'error'].includes(phase) ? 'Arrêtée' : !status.seeded ? 'Connexion…' : `${duration(config.interval_seconds)} cible`;
    $('cadence-detail').textContent = phase === 'backoff'
      ? `Nouvel essai ${secondsUntilRetry === null ? 'en attente' : `dans ${integer.format(secondsUntilRetry)} s`} · cible ${duration(config.interval_seconds)}`
      : ['blocked', 'error'].includes(phase) ? 'aucune surveillance effective'
      : `Cadence cible hors erreurs${number(status.last_attempt_interval_seconds) === null ? '' : ` · dernier intervalle ${duration(status.last_attempt_interval_seconds)}`}`;
    $('new-count').textContent = integer.format(offers.filter((offer) => offer.kind !== 'baseline').length);
    const attemptDuration = number(status.last_attempt_ms) ?? number(status.request_ms);
    $('request-duration').replaceChildren(document.createTextNode(attemptDuration === null ? '—' : integer.format(attemptDuration)));
    if (attemptDuration !== null) $('request-duration').append(element('small', '', isDemo ? 'ms simulées' : 'ms'));
    const lead = number(comparison.median_lead_seconds);
    $('median-lead').textContent = duration(lead, true);
    $('median-lead').className = `metric-value accent${lead !== null && lead < 0 ? ' negative' : ''}`;
    const count = number(comparison.count) ?? 0;
    $('comparison-count').textContent = count ? `${integer.format(count)} comparaison${count > 1 ? 's' : ''} manuelle${count > 1 ? 's' : ''}${isDemo ? ' · simulation' : ''}` : 'aucune comparaison manuelle';
    const pendingCount = number(comparison.pending_count) ?? 0;
    $('pending-comparisons').textContent = `${integer.format(pendingCount)} arrivée${pendingCount > 1 ? 's' : ''} en attente de rapprochement${isDemo ? ' · simulation' : ''}`;
    $('offer-count').textContent = integer.format(offers.length);
    $('poll-count').textContent = integer.format(number(status.polls) ?? 0);
    $('success-count').textContent = integer.format(number(status.successful_polls) ?? 0);
    $('failure-count').textContent = integer.format(number(status.failed_polls) ?? 0);
    $('returned-count').textContent = !status.seeded || number(status.returned_count) === null ? '' : `· ${integer.format(status.returned_count)} annonces dans le dernier instantané`;
    $('baseline-info').textContent = status.seeded ? `${integer.format(number(status.baseline_count) ?? 0)} offre(s) au démarrage · sans alerte` : 'Aucune donnée reçue de l’API';
    $('table-wrap').hidden = offers.length === 0;
    $('offers-empty').hidden = offers.length !== 0;
    if (!offers.length) {
      $('offers-empty').querySelector('h3').textContent = failedCollection ? 'La collecte ne fonctionne pas' : status.seeded ? 'À l’affût de la prochaine offre' : 'Connexion à l’API en cours';
      $('empty-message').textContent = failedCollection ? `${status.message || 'Consultez le diagnostic.'} Utilisez « Diagnostic » pour récupérer le détail de l’échec.` : status.seeded ? 'Aucune offre dans la plage de prix. Gardez cette page ouverte pour recevoir les prochaines détections.' : 'Aucune réponse valide pour le moment : la surveillance n’est pas encore opérationnelle.';
    }
    renderOffers(offers, newKeys);
    notifyOffers(newOffers, isDemo);
  }

  async function poll() {
    if (fetchInProgress) return;
    fetchInProgress = true;
    try {
      const response = await fetch('/api/state', { cache: 'no-store', signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
      $('error-banner').hidden = true;
    } catch (error) {
      showError(`Connexion au serveur interrompue (${error.name === 'TimeoutError' ? 'délai dépassé' : error.message}). Nouvelle tentative automatique.`);
      $('phase-label').textContent = 'Serveur inaccessible';
      $('status-dot').className = 'status-dot error';
      $('mode-badge').textContent = 'HORS CONNEXION';
      $('mode-badge').className = 'mode-badge';
    } finally {
      fetchInProgress = false;
      window.setTimeout(() => { void poll(); }, 500);
    }
  }

  if (!('Notification' in window)) {
    $('notification-button').disabled = true;
    $('notification-button').title = 'Les notifications ne sont pas disponibles dans ce navigateur.';
  }
  void poll();
})();
