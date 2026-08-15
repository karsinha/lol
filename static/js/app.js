document.addEventListener("DOMContentLoaded", () => {
    let previousPlayers = new Map();
    let isUpdating      = false;
    let updateInterval  = null;

    const savedRegion = localStorage.getItem('selectedRegion');
    let currentRegion = savedRegion || window.initialRegion || 'euw1';

    const UPDATE_FREQUENCY        = 60000;
    const REQUEST_TIMEOUT         = 10000;
    const CUTOFF_UPDATE_FREQUENCY = 300000;

    const INTERNAL_TOKEN = window.INTERNAL_TOKEN || '';
    const REGION_NAMES   = window.REGION_NAMES   || {};

    const detailCache = new Map();
    const DETAIL_CACHE_TTL = 5 * 60 * 1000;

    const openDesktop = new Set();

    function apiFetch(url, signal) {
        const opts = { headers: { 'X-Internal-Token': INTERNAL_TOKEN } };
        if (signal) opts.signal = signal;
        return fetch(url, opts);
    }

    document.querySelectorAll('#leaderboard-body tr[data-puuid]').forEach(row => {
        if (row.classList.contains('player-card')) {
            previousPlayers.set(row.dataset.puuid, extractPlayerData(row));
        }
    });

    createUpdateIndicator();

    const regionSelect = document.getElementById('region-select');
    if (regionSelect && savedRegion) regionSelect.value = savedRegion;

    updateCutoffs(currentRegion);
    setInterval(() => updateCutoffs(currentRegion), CUTOFF_UPDATE_FREQUENCY);

    if (regionSelect) {
        regionSelect.addEventListener('change', e => {
            currentRegion = e.target.value;
            localStorage.setItem('selectedRegion', currentRegion);
            updateCutoffs(currentRegion);
        });
    }


    function createUpdateIndicator() {
        const el = document.createElement('div');
        el.id = 'update-indicator';
        el.textContent = 'Cargando...';
        document.body.appendChild(el);
    }

    function showUpdateStatus(msg, isError = false, isLoading = false) {
        const el = document.getElementById('update-indicator');
        if (!el) return;
        el.textContent = msg;
        el.style.borderColor = isError   ? 'rgba(239,68,68,0.5)'
                             : isLoading ? 'rgba(167,139,250,0.3)'
                                         : 'rgba(110,231,183,0.4)';
        el.style.color = isError   ? '#fca5a5'
                       : isLoading ? '#9ca3af'
                                   : '#6ee7b7';

        const banner    = document.getElementById('error-banner');
        const bannerMsg = document.getElementById('error-banner-msg');
        if (!banner) return;
        if (isError) {
            if (bannerMsg) bannerMsg.textContent = `Error al actualizar: ${msg.replace('Error: ', '')}. Los datos mostrados pueden estar desactualizados.`;
            banner.style.display = 'flex';
        } else if (!isLoading) {
            banner.style.display = 'none';
        }
    }


    function updateCutoffs(region) {
        apiFetch(`/cutoffs/${region}`)
            .then(r => { if (!r.ok) throw new Error('cutoffs ' + r.status); return r.json(); })
            .then(data => {
                const challEl = document.getElementById('challenger-lp');
                const gmEl    = document.getElementById('grandmaster-lp');
                if (challEl) challEl.textContent = data.challenger ?? '---';
                if (gmEl)    gmEl.textContent    = data.grandmaster ?? '---';
            })
            .catch(() => {
                ['challenger-lp', 'grandmaster-lp'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.textContent = '---';
                });
            });
    }



    function loadPlayerDetail(puuid) {
        const cached = detailCache.get(puuid);
        if (cached && (Date.now() - cached.ts) < DETAIL_CACHE_TTL) {
            return Promise.resolve(cached.data);
        }
        return apiFetch(`/player_detail/${puuid}`)
            .then(r => { if (!r.ok) throw new Error('detail ' + r.status); return r.json(); })
            .then(data => {
                detailCache.set(puuid, { data, ts: Date.now() });
                return data;
            });
    }

    function renderEloChart(container, history) {
        if (!container) return;
        if (!history || history.length < 2) {
            container.innerHTML = '<div class="expand-empty">Todavía no hay suficientes datos para graficar.</div>';
            return;
        }

        const w = 600, h = 120, pad = 6;
        const values = history.map(p => p.v);
        const min = Math.min(...values);
        const max = Math.max(...values);
        const range = (max - min) || 1;

        const points = history.map((p, i) => {
            const x = pad + (i / (history.length - 1)) * (w - pad * 2);
            const y = h - pad - ((p.v - min) / range) * (h - pad * 2);
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');

        const delta = history[history.length - 1].v - history[0].v;
        const deltaCls  = delta > 0 ? 'positive' : delta < 0 ? 'negative' : '';
        const deltaText = `${delta > 0 ? '+' : ''}${delta} LP`;

        container.innerHTML = `
            <div class="elo-chart-header">
                <span>Evolución reciente</span>
                <span class="elo-chart-delta ${deltaCls}">${deltaText}</span>
            </div>
            <svg viewBox="0 0 ${w} ${h}" class="elo-chart-svg" preserveAspectRatio="none">
                <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
            </svg>
        `;
    }

    function renderMatchList(container, matches) {
        if (!container) return;
        if (!matches || matches.length === 0) {
            container.innerHTML = '<div class="expand-empty">No se encontraron partidas recientes.</div>';
            return;
        }

        container.innerHTML = matches.map(m => `
            <div class="match-row ${m.win ? 'win' : 'loss'}">
                <span class="match-result">${m.win ? 'Victoria' : 'Derrota'}</span>
                <span class="match-champ">${m.champion}</span>
                <span class="match-kda">${m.kills}/${m.deaths}/${m.assists}</span>
                <span class="match-kp">${m.kp}% KP</span>
                <span class="match-cs">${m.cs} CS</span>
                <span class="match-duration">${m.duration_min}m</span>
            </div>
        `).join('');
    }

    function wireTabs(panel) {
        panel.querySelectorAll('.expand-tab').forEach(tabBtn => {
            tabBtn.onclick = () => {
                panel.querySelectorAll('.expand-tab').forEach(b => b.classList.remove('active'));
                tabBtn.classList.add('active');
                panel.querySelectorAll('.expand-body').forEach(b => b.hidden = true);
                const target = panel.querySelector(`[data-tab-content="${tabBtn.dataset.tab}"]`);
                if (target) target.hidden = false;
            };
        });
    }

    function hydratePanel(panel, puuid, showLoading) {
        const eloBody  = panel.querySelector('[data-tab-content="elo"] .elo-chart-wrap');
        const histBody = panel.querySelector('[data-tab-content="history"] .match-list');

        if (showLoading) {
            if (eloBody)  eloBody.innerHTML  = '<div class="expand-loading">Cargando...</div>';
            if (histBody) histBody.innerHTML = '<div class="expand-loading">Cargando...</div>';
        }

        loadPlayerDetail(puuid).then(data => {
            renderEloChart(eloBody, data.elo_history);
            renderMatchList(histBody, data.matches);
        }).catch(() => {
            if (eloBody)  eloBody.innerHTML  = '<div class="expand-empty">Error cargando datos.</div>';
            if (histBody) histBody.innerHTML = '<div class="expand-empty">Error cargando datos.</div>';
        });
    }



    document.getElementById('leaderboard-body')?.addEventListener('click', e => {
        const btn = e.target.closest('.expand-toggle');
        if (!btn) return;

        const puuid = btn.dataset.puuid;
        const expandRow = document.querySelector(`tr.expand-row[data-puuid="${puuid}"]`);
        if (!expandRow) return;

        const isOpen = !expandRow.hidden;

        if (isOpen) {
            expandRow.hidden = true;
            btn.classList.remove('open');
            openDesktop.delete(puuid);
            return;
        }

        expandRow.hidden = false;
        btn.classList.add('open');
        openDesktop.add(puuid);

        const panel = expandRow.querySelector('.expand-panel');
        wireTabs(panel);
        hydratePanel(panel, puuid, true);
    });



    document.getElementById('mobile-list')?.addEventListener('toggle', e => {
        const card = e.target;
        if (!card.classList || !card.classList.contains('mobile-card')) return;
        if (!card.open) return;

        const puuid = card.dataset.puuid;
        const panel = card.querySelector('.mc-detail');
        if (!panel) return;

        wireTabs(panel);
        hydratePanel(panel, puuid, true);
    }, true);


    function extractPlayerData(row) {
        const gains = row.querySelectorAll('td.lp-gain');
        return {
            tier:        row.querySelector('.tier-name')?.textContent?.trim()    || '',
            rank:        row.querySelector('.rank-div')?.textContent?.trim()     || '',
            lp:          row.querySelector('.lp-highlight')?.textContent?.trim() || '',
            wins:        row.querySelector('.w-text')?.textContent?.trim()       || '',
            losses:      row.querySelector('.l-text')?.textContent?.trim()       || '',
            winrate:     row.querySelector('.wr-bar-fill')?.style.width          || '0%',
            is_playing:  !!row.querySelector('.ingame-status'),
            mode:        row.querySelector('.ingame-status span:not(.dot)')?.textContent?.trim() || '',
            time:        row.querySelector('.game-time')?.textContent?.replace(/[()]/g, '').trim() || '',
            hot_streak:  !!row.querySelector('.hot-streak'),
            lp_gain_24h: gains[0]?.dataset.raw ?? '',
            lp_gain_7d:  gains[1]?.dataset.raw ?? '',
        };
    }

    function hasChanged(old, neo) {
        if (!old) return true;
        return ['tier', 'rank', 'lp', 'wins', 'losses', 'winrate',
                'is_playing', 'mode', 'time', 'hot_streak',
                'lp_gain_24h', 'lp_gain_7d']
            .some(k => old[k] !== neo[k]);
    }

    function formatGain(raw) {
        if (raw === null || raw === undefined || raw === '') return { text: '—', cls: '' };
        const n = Number(raw);
        return {
            text: `${n > 0 ? '+' : ''}${n} LP`,
            cls:  n > 0 ? 'positive' : n < 0 ? 'negative' : ''
        };
    }


    function updateRow(row, player, pos, total, old) {
        const rowClass = pos <= 4 ? 'top-zone' : pos > total - 4 ? 'danger-zone' : '';

        if (row.dataset.pos !== String(pos)) {
            row.dataset.pos = pos;
            row.className = `player-card ${rowClass}`.trim();
            const posText = row.querySelector('.expand-pos');
            if (posText) posText.textContent = pos;
        }

        if (!hasChanged(old, buildComparable(player))) return;

        const rankImg = row.querySelector('.rank-icon');
        if (rankImg && rankImg.src !== player.emblem_url) {
            rankImg.src = player.emblem_url;
            rankImg.alt = player.tier;
        }
        const tierEl = row.querySelector('.tier-name');
        const rankEl = row.querySelector('.rank-div');
        if (tierEl) tierEl.textContent = player.tier;
        if (rankEl) rankEl.textContent = player.rank;

        const lpEl = row.querySelector('.lp-highlight');
        if (lpEl) lpEl.textContent = `${player.lp} LP`;

        const wEl = row.querySelector('.w-text');
        const lEl = row.querySelector('.l-text');
        if (wEl) wEl.textContent = `${player.wins}W`;
        if (lEl) lEl.textContent = `${player.losses}L`;

        const fill  = row.querySelector('.wr-bar-fill');
        const pctEl = row.querySelector('.wr-pct');
        if (fill) {
            fill.style.width = `${player.winrate}%`;
            fill.className   = `wr-bar-fill ${player.winrate > 50 ? 'wr-green' : player.winrate < 50 ? 'wr-red' : 'wr-gray'}`;
        }
        if (pctEl) pctEl.textContent = `${player.winrate}%`;

        const namesDiv  = row.querySelector('.real-name');
        const hotExists = !!row.querySelector('.hot-streak');
        if (player.hot_streak && !hotExists) {
            const span = document.createElement('span');
            span.className   = 'hot-streak';
            span.title       = 'Hot streak (+3)';
            span.textContent = '🔥';
            namesDiv?.appendChild(span);
        } else if (!player.hot_streak && hotExists) {
            row.querySelector('.hot-streak')?.remove();
        }

        const statusTd = row.querySelector('td:nth-child(7)');
        if (statusTd) {
            statusTd.innerHTML = player.is_playing
                ? `<div class="ingame-status"><span class="dot"></span> ${player.mode} <span class="game-time">(${player.time})</span></div>`
                : `<span class="offline-text">Offline</span>`;
        }

        const gainTds = row.querySelectorAll('td.lp-gain');
        const g24 = formatGain(player.lp_gain_24h);
        const g7  = formatGain(player.lp_gain_7d);
        if (gainTds[0]) {
            gainTds[0].textContent = g24.text;
            gainTds[0].className   = `lp-gain ${g24.cls}`.trim();
            gainTds[0].dataset.raw = player.lp_gain_24h ?? '';
        }
        if (gainTds[1]) {
            gainTds[1].textContent = g7.text;
            gainTds[1].className   = `lp-gain ${g7.cls}`.trim();
            gainTds[1].dataset.raw = player.lp_gain_7d ?? '';
        }
    }


    function buildComparable(p) {
        return {
            tier:        p.tier   || '',
            rank:        p.rank   || '',
            lp:          String(p.lp) + ' LP',
            wins:        String(p.wins) + 'W',
            losses:      String(p.losses) + 'L',
            winrate:     `${p.winrate}%`,
            is_playing:  p.is_playing,
            mode:        p.mode   || '',
            time:        p.time   || '',
            hot_streak:  p.hot_streak,
            lp_gain_24h: String(p.lp_gain_24h ?? ''),
            lp_gain_7d:  String(p.lp_gain_7d  ?? ''),
        };
    }


    function createRowHTML(player, pos, total) {
        const wrClass    = player.winrate > 50 ? 'wr-green' : player.winrate < 50 ? 'wr-red' : 'wr-gray';
        const g24        = formatGain(player.lp_gain_24h);
        const g7         = formatGain(player.lp_gain_7d);
        const friendly   = REGION_NAMES[(player.region || '').toLowerCase()] || player.region;
        const hotHTML    = player.hot_streak ? `<span class="hot-streak" title="Hot streak (+3)">🔥</span>` : '';
        const statusHTML = player.is_playing
            ? `<div class="ingame-status"><span class="dot"></span> ${player.mode} <span class="game-time">(${player.time})</span></div>`
            : `<span class="offline-text">Offline</span>`;

        return `
            <td class="pos-cell">
                <button type="button" class="expand-toggle" data-puuid="${player.puuid}" aria-label="Ver detalle">
                    <span class="expand-pos">${pos}</span>
                    <span class="expand-arrow">▾</span>
                </button>
            </td>
            <td>
                <div class="player-info">
                    <div class="player-img-wrapper">
                        <img src="${player.icon_url}" alt="${player.person_name}" loading="lazy">
                    </div>
                    <div class="player-names">
                        <div class="real-name">${player.person_name}${hotHTML}</div>
                        <a href="${player.opgg_url}" target="_blank" class="account-name">
                            ${player.game_name}<span class="tag-span"> #${player.tag_line}</span>
                        </a>
                        <small class="region-badge">${friendly}</small>
                    </div>
                </div>
            </td>
            <td>
                <div class="rank-wrapper">
                    <img src="${player.emblem_url}" class="rank-icon" alt="${player.tier}" loading="lazy">
                    <div>
                        <span class="tier-name">${player.tier}</span>
                        <span class="rank-div">${player.rank}</span>
                    </div>
                </div>
            </td>
            <td class="lp-highlight">${player.lp} LP</td>
            <td class="wl-cell">
                <span class="w-text">${player.wins}W</span>
                <span class="sep">/</span>
                <span class="l-text">${player.losses}L</span>
            </td>
            <td>
                <div class="wr-wrap">
                    <div class="wr-bar-bg">
                        <div class="wr-bar-fill ${wrClass}" style="width:${player.winrate}%;"></div>
                    </div>
                    <span class="wr-pct">${player.winrate}%</span>
                </div>
            </td>
            <td>${statusHTML}</td>
            <td class="lp-gain ${g24.cls}" data-raw="${player.lp_gain_24h ?? ''}">${g24.text}</td>
            <td class="lp-gain ${g7.cls}"  data-raw="${player.lp_gain_7d  ?? ''}">${g7.text}</td>
        `;
    }

    function createExpandRowHTML(puuid) {
        return `
            <td colspan="9">
                <div class="expand-panel">
                    <div class="expand-tabs">
                        <button type="button" class="expand-tab active" data-tab="elo">Stats &amp; Elo</button>
                        <button type="button" class="expand-tab" data-tab="history">Historial</button>
                    </div>
                    <div class="expand-body" data-tab-content="elo">
                        <div class="elo-chart-wrap"><div class="expand-loading">Cargando...</div></div>
                    </div>
                    <div class="expand-body" data-tab-content="history" hidden>
                        <div class="match-list"><div class="expand-loading">Cargando...</div></div>
                    </div>
                </div>
            </td>
        `;
    }


    function reorderRows(tbody, orderedPairs) {
        const flat = orderedPairs.flat();
        flat.forEach((row, i) => {
            if (tbody.children[i] !== row) tbody.insertBefore(row, tbody.children[i] || null);
        });
    }


    function updateLeaderboard() {
        if (isUpdating) return;
        isUpdating = true;
        showUpdateStatus('Actualizando...', false, true);

        const controller = new AbortController();
        const timeoutId  = setTimeout(() => controller.abort(), REQUEST_TIMEOUT);

        apiFetch('/update_data', controller.signal)
            .then(r => {
                clearTimeout(timeoutId);
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then(players => {
                const tbody  = document.getElementById('leaderboard-body');
                const newMap = new Map(players.map(p => [p.puuid, p]));
                const total  = players.length;

                for (const puuid of previousPlayers.keys()) {
                    if (!newMap.has(puuid)) {
                        tbody.querySelector(`tr.player-card[data-puuid="${puuid}"]`)?.remove();
                        tbody.querySelector(`tr.expand-row[data-puuid="${puuid}"]`)?.remove();
                        openDesktop.delete(puuid);
                    }
                }

                const orderedPairs = players.map((player, i) => {
                    const pos = i + 1;
                    const old = previousPlayers.get(player.puuid);
                    let   row = tbody.querySelector(`tr.player-card[data-puuid="${player.puuid}"]`);
                    let   expandRow = tbody.querySelector(`tr.expand-row[data-puuid="${player.puuid}"]`);

                    if (!row) {
                        row = document.createElement('tr');
                        row.className     = `player-card`;
                        row.dataset.puuid = player.puuid;
                        row.dataset.pos   = pos;
                        row.innerHTML     = createRowHTML(player, pos, total);

                        expandRow = document.createElement('tr');
                        expandRow.className     = 'expand-row';
                        expandRow.dataset.puuid = player.puuid;
                        expandRow.hidden        = true;
                        expandRow.innerHTML     = createExpandRowHTML(player.puuid);
                    } else {
                        updateRow(row, player, pos, total, old);
                    }

                    if (openDesktop.has(player.puuid) && expandRow) {
                        expandRow.hidden = false;
                        const toggleBtn = row.querySelector('.expand-toggle');
                        toggleBtn?.classList.add('open');
                        const panel = expandRow.querySelector('.expand-panel');
                        if (panel) {
                            wireTabs(panel);
                            hydratePanel(panel, player.puuid, false);
                        }
                    }

                    return [row, expandRow];
                });

                reorderRows(tbody, orderedPairs);

                updateMobileList(players);
                previousPlayers = newMap;

                const now = new Date();
                showUpdateStatus(`Actualizado ${now.toLocaleTimeString()}`, false, false);
            })
            .catch(err => {
                clearTimeout(timeoutId);
                showUpdateStatus(
                    `Error: ${err.name === 'AbortError' ? 'Timeout' : err.message}`,
                    true, false
                );
            })
            .finally(() => { isUpdating = false; });
    }


    function updateMobileList(players) {
        const list = document.getElementById('mobile-list');
        if (!list) return;
        const total = players.length;


        const opened = new Set(
            [...list.querySelectorAll('details[open]')]
                .map(c => c.dataset.puuid)
        );

        list.innerHTML = players.map((p, i) => {
            const pos        = i + 1;
            const cls        = pos <= 4 ? 'top-zone' : pos > total - 4 ? 'danger-zone' : '';
            const isOpen     = opened.has(p.puuid) ? 'open' : '';
            const tier       = `${p.tier} ${p.rank}`.trim();
            const g24        = formatGain(p.lp_gain_24h);
            const g7         = formatGain(p.lp_gain_7d);
            const wrFill     = p.winrate > 50
                ? 'linear-gradient(90deg,#10b981,#34d399)'
                : p.winrate < 50
                    ? 'linear-gradient(90deg,#ef4444,#f87171)'
                    : '#475569';
            const friendly   = REGION_NAMES[(p.region || '').toLowerCase()] || p.region;
            const statusHTML = p.is_playing
                ? `<span class="m-ingame"><span class="dot"></span> ${p.mode}</span>`
                : `<span class="mc-detail-value muted">Offline</span>`;

            return `
            <details class="mobile-card ${cls}" data-puuid="${p.puuid}" ${isOpen}>
                <summary class="mc-main">
                    <span class="m-pos">${pos}</span>
                    <img class="m-avatar" src="${p.icon_url}" alt="${p.person_name}" loading="lazy">
                    <div class="m-info">
                        <div class="m-person">${p.person_name}${p.hot_streak ? ' 🔥' : ''} <span class="region-badge">${friendly}</span></div>
                        <div class="m-name">${p.game_name}<span class="m-tag"> #${p.tag_line}</span></div>
                    </div>
                    <div class="m-right">
                        <div class="m-rank">
                            <img class="m-rank-icon" src="${p.emblem_url}" alt="${p.tier}" loading="lazy">
                            <span class="m-tier">${tier}</span>
                        </div>
                        <span class="m-lp">${p.lp} LP</span>
                    </div>
                    <span class="mc-chevron">▾</span>
                </summary>
                <div class="mc-detail">
                    <div class="mc-detail-row">
                        <span class="mc-detail-label">W / L</span>
                        <span class="mc-detail-value"><span style="color:var(--win)">${p.wins}W</span> / <span style="color:var(--loss)">${p.losses}L</span></span>
                    </div>
                    <div class="mc-detail-row">
                        <span class="mc-detail-label">Winrate</span>
                        <div class="mc-detail-wr">
                            <div class="mc-wr-bar-bg"><div class="mc-wr-bar-fill" style="width:${p.winrate}%;background:${wrFill}"></div></div>
                            <span class="mc-detail-value">${p.winrate}%</span>
                        </div>
                    </div>
                    <div class="mc-detail-row">
                        <span class="mc-detail-label">Estado</span>
                        ${statusHTML}
                    </div>
                    <div class="mc-detail-row">
                        <span class="mc-detail-label">24h</span>
                        <span class="mc-detail-value ${g24.cls}">${g24.text}</span>
                    </div>
                    <div class="mc-detail-row">
                        <span class="mc-detail-label">7 días</span>
                        <span class="mc-detail-value ${g7.cls}">${g7.text}</span>
                    </div>

                    <div class="expand-tabs">
                        <button type="button" class="expand-tab active" data-tab="elo">Stats &amp; Elo</button>
                        <button type="button" class="expand-tab" data-tab="history">Historial</button>
                    </div>
                    <div class="expand-body" data-tab-content="elo">
                        <div class="elo-chart-wrap"><div class="expand-loading">Cargando...</div></div>
                    </div>
                    <div class="expand-body" data-tab-content="history" hidden>
                        <div class="match-list"><div class="expand-loading">Cargando...</div></div>
                    </div>

                    <a href="${p.opgg_url}" target="_blank" class="mc-opgg-link">↗ Ver en op.gg</a>
                </div>
            </details>`;
        }).join('');

        opened.forEach(puuid => {
            const card = list.querySelector(`.mobile-card[data-puuid="${puuid}"]`);
            const panel = card?.querySelector('.mc-detail');
            if (panel) {
                wireTabs(panel);
                hydratePanel(panel, puuid, false);
            }
        });
    }


    function scheduleFirstUpdate() {
        const start = () => {
            updateLeaderboard();
            updateInterval = setInterval(updateLeaderboard, UPDATE_FREQUENCY);
        };


        if ('requestIdleCallback' in window) {
            requestIdleCallback(start, { timeout: 3000 });
        } else {
            setTimeout(start, 2000);
        }
    }

    scheduleFirstUpdate();

});