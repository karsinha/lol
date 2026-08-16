document.addEventListener("DOMContentLoaded", () => {
    let previousPlayers = new Map();
    let isUpdating      = false;
    let updateInterval  = null;
    let hasRealData = document.querySelectorAll('#leaderboard-body tr.player-card').length > 0;

    const savedRegion = localStorage.getItem('selectedRegion');
    let currentRegion = savedRegion || window.initialRegion || 'euw1';

    const AVATAR_PLACEHOLDER = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 112 112'%3E%3Crect width='112' height='112' rx='24' fill='%2313132a'/%3E%3Ccircle cx='56' cy='44' r='20' fill='%23475569'/%3E%3Cpath d='M20 96c4-24 26-36 36-36s32 12 36 36' fill='%23475569'/%3E%3C/svg%3E";

    function escapeHTML(str) {
    if (str === null || str === undefined) return '';
    return String(str).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

    const UPDATE_FREQUENCY        = 60000;
    const REQUEST_TIMEOUT         = 10000;
    const CUTOFF_UPDATE_FREQUENCY = 300000;

    const INTERNAL_TOKEN = window.INTERNAL_TOKEN || '';
    const REGION_NAMES   = window.REGION_NAMES   || {};

    const detailCache = new Map();
    const DETAIL_CACHE_TTL = 5 * 60 * 1000;

    const gameTimerInterval_MS = 15000; 
    let gameTimerInterval = null;

    function formatElapsed(ms) {
        const totalMin = Math.max(0, Math.floor(ms / 60000));
        return `(${totalMin} min)`;
    }

    function tickGameTimers() {
        const now = Date.now();
        document.querySelectorAll('[data-game-start]').forEach(el => {
            const start = Number(el.dataset.gameStart);
            if (!start) return;
            const timeEl = el.querySelector('.game-time');
            if (timeEl) timeEl.textContent = formatElapsed(now - start);
        });
    }

    function ensureGameTimerLoop() {
        if (gameTimerInterval) return;
        tickGameTimers();
        gameTimerInterval = setInterval(tickGameTimers, gameTimerInterval_MS);
    }

    ensureGameTimerLoop();

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

    function formatAxisDate(tStr) {
        const d = new Date(tStr.replace(' ', 'T'));
        const months = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
        return `${d.getDate()} ${months[d.getMonth()]}`;
    }

    function pickAxisTicks(history) {
        const n = history.length;
        if (n <= 1) return [];
        if (n === 2) return [0, 1];

        const firstT = new Date(history[0].t.replace(' ', 'T')).getTime();
        const lastT  = new Date(history[n - 1].t.replace(' ', 'T')).getTime();
        const spanDays = Math.max(1, (lastT - firstT) / 86400000);
        const maxLabels = 6;
        const stepMs = Math.max(3, Math.ceil(spanDays / maxLabels)) * 86400000;

        const indices = [0];
        let lastPicked = firstT;
        for (let i = 1; i < n; i++) {
            const t = new Date(history[i].t.replace(' ', 'T')).getTime();
            if (t - lastPicked >= stepMs) {
                indices.push(i);
                lastPicked = t;
            }
        }
        if (indices[indices.length - 1] !== n - 1) indices.push(n - 1);
        return indices;
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
        const lastIndex = history.length - 1;

        const points = history.map((p, i) => {
            const x = pad + (i / lastIndex) * (w - pad * 2);
            const y = h - pad - ((p.v - min) / range) * (h - pad * 2);
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');

        const delta = history[history.length - 1].v - history[0].v;
        const deltaCls  = delta > 0 ? 'positive' : delta < 0 ? 'negative' : '';
        const deltaText = `${delta > 0 ? '+' : ''}${delta} LP`;

        const axisHTML = pickAxisTicks(history).map(i => {
            const pct   = (i / lastIndex) * 100;
            const align = i === 0 ? 'left' : i === lastIndex ? 'right' : 'center';
            return `<span class="elo-axis-tick" data-align="${align}" style="left:${pct.toFixed(2)}%;">${formatAxisDate(history[i].t)}</span>`;
        }).join('');

        container.innerHTML = `
            <div class="elo-chart-header">
                <span>Evolución reciente</span>
                <span class="elo-chart-delta ${deltaCls}">${deltaText}</span>
            </div>
            <svg viewBox="0 0 ${w} ${h}" class="elo-chart-svg" preserveAspectRatio="none">
                <polyline points="${points}" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
            </svg>
            <div class="elo-chart-axis">${axisHTML}</div>
        `;
    }


    const DDRAGON_CACHE_KEY = 'ddragonCache_v1';
    const DDRAGON_CACHE_TTL = 24 * 60 * 60 * 1000; 

    let DDRAGON_VERSION = '14.23.1';
    let RUNE_ICONS = {};

    const SPELL_ICON_MAP = {
        1: 'SummonerBoost', 3: 'SummonerExhaust', 4: 'SummonerFlash',
        6: 'SummonerHaste', 7: 'SummonerHeal', 11: 'SummonerSmite',
        12: 'SummonerTeleport', 13: 'SummonerMana', 14: 'SummonerDot',
        21: 'SummonerBarrier', 30: 'SummonerPoroRecall', 31: 'SummonerPoroThrow',
        32: 'SummonerSnowball', 39: 'SummonerSnowURFSnowball_Mark'
    };

    function readDdragonCache() {
        try {
            const raw = localStorage.getItem(DDRAGON_CACHE_KEY);
            if (!raw) return null;

            const parsed = JSON.parse(raw);
            if (!parsed || !parsed.ts || !parsed.version || !parsed.runeIcons) return null;

            if ((Date.now() - parsed.ts) > DDRAGON_CACHE_TTL) return null; 

            return parsed;
        } catch {
            return null;
        }
    }

    function writeDdragonCache(version, runeIcons) {
        try {
            localStorage.setItem(DDRAGON_CACHE_KEY, JSON.stringify({
                ts: Date.now(),
                version,
                runeIcons
            }));
        } catch {
        }
    }

    function buildRuneIconsFromTrees(trees) {
        const runeIcons = {};
        trees.forEach(tree => {
            runeIcons[tree.id] = tree.icon;
            tree.slots.forEach(slot => slot.runes.forEach(rune => {
                runeIcons[rune.id] = rune.icon;
            }));
        });
        return runeIcons;
    }

    function loadDdragonData() {
        const cached = readDdragonCache();

        if (cached) {
            DDRAGON_VERSION = cached.version;
            RUNE_ICONS = cached.runeIcons;
            return; 
        }

        fetch('https://ddragon.leagueoflegends.com/api/versions.json')
            .then(r => r.ok ? r.json() : Promise.reject())
            .then(versions => {
                if (Array.isArray(versions) && versions[0]) DDRAGON_VERSION = versions[0];
            })
            .catch(() => {})
            .finally(() => {
                fetch(`https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VERSION}/data/en_US/runesReforged.json`)
                    .then(r => r.ok ? r.json() : Promise.reject())
                    .then(trees => {
                        RUNE_ICONS = buildRuneIconsFromTrees(trees);
                        writeDdragonCache(DDRAGON_VERSION, RUNE_ICONS);
                    })
                    .catch(() => {});
            });
    }

    loadDdragonData();

    function champIconUrl(champion) {
        return `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VERSION}/img/champion/${champion}.png`;
    }

    function spellIconUrl(id) {
        const key = SPELL_ICON_MAP[id];
        return key ? `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VERSION}/img/spell/${key}.png` : '';
    }

    function runeIconUrl(id) {
        const icon = RUNE_ICONS[id];
        return icon ? `https://ddragon.leagueoflegends.com/cdn/img/${icon}` : '';
    }

    function itemIconUrl(id) {
        return id ? `https://ddragon.leagueoflegends.com/cdn/${DDRAGON_VERSION}/img/item/${id}.png` : '';
    }

    function timeAgo(epochMs) {
        if (!epochMs) return '';
        const diffMin = Math.max(0, Math.round((Date.now() - epochMs) / 60000));
        if (diffMin < 60) return `hace ${diffMin} min`;
        const diffH = Math.round(diffMin / 60);
        if (diffH < 24) return `hace ${diffH} h`;
        return `hace ${Math.round(diffH / 24)} d`;
    }

    function renderMatchList(container, matches) {
    if (!container) return;
    if (!matches || matches.length === 0) {
        container.innerHTML = '<div class="expand-empty">No se encontraron partidas recientes.</div>';
        return;
    }

    container.innerHTML = matches.map(m => {
        const kdaRatio = (((m.kills + m.assists) / Math.max(1, m.deaths))).toFixed(1);
        const hasLp    = m.lp_change !== null && m.lp_change !== undefined;
        const lpCls    = hasLp ? (m.lp_change > 0 ? 'positive' : m.lp_change < 0 ? 'negative' : '') : '';
        const lpText   = hasLp ? `${m.lp_change > 0 ? '+' : ''}${m.lp_change} LP` : '—';

        const spell1Url  = spellIconUrl(m.spell1);
        const spell2Url  = spellIconUrl(m.spell2);
        const runeUrl    = runeIconUrl(m.rune_primary);
        const subRuneUrl = runeIconUrl(m.rune_sub);

        const items = Array.isArray(m.items) ? m.items : [];
        const itemsHTML = Array.from({ length: 7 }).map((_, i) => {
            const id = items[i];
            return id
                ? `<img class="match-item-icon" src="${itemIconUrl(id)}" alt="" loading="lazy">`
                : `<span class="match-item-icon empty"></span>`;
        }).join('');

        const queueCls = m.queue_id === 420 ? 'ranked-solo'
                        : m.queue_id === 440 ? 'ranked-flex'
                        : m.queue_id === 450 ? 'aram'
                        : 'other';

        return `
            <div class="match-row ${m.win ? 'win' : 'loss'}">
                <div class="match-champ-block">
                    <img class="match-champ-icon" src="${champIconUrl(m.champion)}" alt="${m.champion}" title="${m.champion}" loading="lazy">
                    <div class="match-loadout">
                        <div class="match-spells">
                            ${spell1Url ? `<img class="match-spell-icon" src="${spell1Url}" alt="" loading="lazy">` : ''}
                            ${spell2Url ? `<img class="match-spell-icon" src="${spell2Url}" alt="" loading="lazy">` : ''}
                        </div>
                        <div class="match-runes">
                            ${runeUrl ? `<img class="match-rune-icon" src="${runeUrl}" alt="" loading="lazy">` : ''}
                            ${subRuneUrl ? `<img class="match-rune-icon sub" src="${subRuneUrl}" alt="" loading="lazy">` : ''}
                        </div>
                    </div>
                </div>
                <div class="match-main">
                    <span class="match-result">${m.win ? 'Victoria' : 'Derrota'}</span>
                    <span class="match-queue-badge ${queueCls}">${m.queue}</span>
                    <span class="match-time-ago">${timeAgo(m.game_creation)}</span>
                </div>
                <div class="match-kda-block">
                    <span class="match-kda">${m.kills}/${m.deaths}/${m.assists}</span>
                    <span class="match-kda-ratio">${kdaRatio} KDA</span>
                </div>
                <div class="match-meta">
                    <span class="match-kp">${m.kp}% KP</span>
                    <span class="match-cs">${m.cs} CS</span>
                </div>
                <div class="match-items">${itemsHTML}</div>
                <span class="match-duration">${m.duration_min}m</span>
                <span class="match-lp ${lpCls}">${lpText}</span>
            </div>
        `;
    }).join('');
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
        if (e.target.closest('a')) return;
        if (e.target.closest('.expand-row')) return;

        const row = e.target.closest('tr.player-card');
        if (!row) return;

        const puuid = row.dataset.puuid;
        const btn = row.querySelector('.expand-toggle');
        const expandRow = document.querySelector(`tr.expand-row[data-puuid="${puuid}"]`);
        if (!expandRow) return;

        const isOpen = !expandRow.hidden;

        if (isOpen) {
            expandRow.hidden = true;
            btn?.classList.remove('open');
            openDesktop.delete(puuid);
            return;
        }

        expandRow.hidden = false;
        btn?.classList.add('open');
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
            mode:        row.querySelector('.mode-text')?.textContent?.trim() || '',
            hot_streak:  !!row.querySelector('.hot-streak'),
            lp_gain_24h: gains[0]?.dataset.raw ?? '',
        };
    }

    function hasChanged(old, neo) {
        if (!old) return true;
        return ['tier', 'rank', 'lp', 'wins', 'losses', 'winrate',
                'is_playing', 'mode',  'hot_streak',
                'lp_gain_24h']
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
                ? `<div class="ingame-status" data-game-start="${player.game_start_epoch_ms || ''}"><span class="dot"></span> <span class="mode-text">${player.mode}</span> <span class="game-time"></span></div>`
                : `<span class="offline-text">Offline</span>`;
        }

        const gainTds = row.querySelectorAll('td.lp-gain');
        const g24 = formatGain(player.lp_gain_24h);
        if (gainTds[0]) {
            gainTds[0].textContent = g24.text;
            gainTds[0].className   = `lp-gain ${g24.cls}`.trim();
            gainTds[0].dataset.raw = player.lp_gain_24h ?? '';
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
        };
    }


    function createRowHTML(player, pos, total) {
    const wrClass    = player.winrate > 50 ? 'wr-green' : player.winrate < 50 ? 'wr-red' : 'wr-gray';
    const g24        = formatGain(player.lp_gain_24h);
    const friendly   = REGION_NAMES[(player.region || '').toLowerCase()] || player.region;
    const hotHTML    = player.hot_streak ? `<span class="hot-streak" title="Hot streak (+3)">🔥</span>` : '';
    const statusHTML = player.is_playing
        ? `<div class="ingame-status" data-game-start="${player.game_start_epoch_ms || ''}"><span class="dot"></span> <span class="mode-text">${escapeHTML(player.mode)}</span> <span class="game-time"></span></div>`
        : `<span class="offline-text">Offline</span>`;

    const personName = escapeHTML(player.person_name);
    const gameName   = escapeHTML(player.game_name);
    const tagLine    = escapeHTML(player.tag_line);
    const tier       = escapeHTML(player.tier);
    const opggUrl    = escapeHTML(player.opgg_url);
    const iconUrl    = escapeHTML(player.icon_url);
    const emblemUrl  = escapeHTML(player.emblem_url);

    return `
        <td class="pos-cell">
            <button type="button" class="expand-toggle" data-puuid="${player.puuid}" aria-label="Ver detalle">
                <span class="expand-pos">${pos}</span>
                <span class="expand-arrow">▾</span>
            </button>
        </td>
        <td>
<div class="player-info">
                <img class="player-avatar" src="${iconUrl}" alt="${personName}" loading="lazy" onerror="this.onerror=null;this.src='${AVATAR_PLACEHOLDER}'">
    <div class="player-names">
        <div class="real-name">${personName}${hotHTML}</div>
        <span class="account-name">
            ${gameName}<span class="tag-span"> #${tagLine}</span>
        </span>
        <small class="region-badge">${escapeHTML(friendly)}</small>
    </div>
</div>
</td>
        <td>
            <div class="rank-wrapper">
                <img src="${emblemUrl}" class="rank-icon" alt="${tier}" loading="lazy">
                <div>
                    <span class="tier-name">${tier}</span>
                    <span class="rank-div">${escapeHTML(player.rank)}</span>
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
<td class="opgg-cell">
    <a href="${opggUrl}" target="_blank" class="opgg-btn" aria-label="Ver en op.gg" title="Ver en op.gg">OP.GG ↗</a>
</td>
    `;
}

    function createExpandRowHTML(puuid) {
        return `
            <td colspan="9">
                <div class="expand-panel">
                    <div class="expand-tabs">
                        <button type="button" class="expand-tab active" data-tab="elo">Gráfico</button>
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
                if (r.status === 304) return null; 
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then(players => {
                if (players === null) {
                    const now = new Date();
                    showUpdateStatus(`Actualizado ${now.toLocaleTimeString()}`, false, false);
                    return;
                }

                hasRealData = players.length > 0;   

                if (hasRealData) {
                    document.getElementById('loading-placeholder-row')?.remove();
                }


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
                tickGameTimers();

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
    const tier       = escapeHTML(`${p.tier} ${p.rank}`.trim());
    const g24        = formatGain(p.lp_gain_24h);
    const wrFill     = p.winrate > 50
        ? 'linear-gradient(90deg,#10b981,#34d399)'
        : p.winrate < 50
            ? 'linear-gradient(90deg,#ef4444,#f87171)'
            : '#475569';
    const friendly   = escapeHTML(REGION_NAMES[(p.region || '').toLowerCase()] || p.region);
    const statusHTML = p.is_playing
        ? `<span class="m-ingame"><span class="dot"></span> ${escapeHTML(p.mode)}</span>`
        : `<span class="mc-detail-value muted">Offline</span>`;

    const personName = escapeHTML(p.person_name);
    const gameName   = escapeHTML(p.game_name);
    const tagLine    = escapeHTML(p.tag_line);
    const opggUrl    = escapeHTML(p.opgg_url);
    const iconUrl    = escapeHTML(p.icon_url);
    const emblemUrl  = escapeHTML(p.emblem_url);
    const tierRaw    = escapeHTML(p.tier);

    return `
    <details class="mobile-card ${cls}" data-puuid="${p.puuid}" ${isOpen}>
        <summary class="mc-main">
            <span class="m-pos">${pos}</span>
                <img class="m-avatar" src="${iconUrl}" alt="${personName}" loading="lazy" onerror="this.onerror=null;this.src='${AVATAR_PLACEHOLDER}'">
            <div class="m-info">
                <div class="m-person">${personName}${p.hot_streak ? ' 🔥' : ''} <span class="region-badge">${friendly}</span></div>
                <div class="m-name">${gameName}<span class="m-tag"> #${tagLine}</span></div>
            </div>
            <div class="m-right">
                <div class="m-rank">
                    <img class="m-rank-icon" src="${emblemUrl}" alt="${tierRaw}" loading="lazy">
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

            <div class="expand-tabs">
                <button type="button" class="expand-tab active" data-tab="elo">Gráfico</button>
                <button type="button" class="expand-tab" data-tab="history">Historial</button>
            </div>
            <div class="expand-body" data-tab-content="elo">
                <div class="elo-chart-wrap"><div class="expand-loading">Cargando...</div></div>
            </div>
            <div class="expand-body" data-tab-content="history" hidden>
                <div class="match-list"><div class="expand-loading">Cargando...</div></div>
            </div>

            <a href="${opggUrl}" target="_blank" class="mc-opgg-link">↗ Ver en op.gg</a>
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


    const FAST_POLL_INTERVAL = 3000; 

function scheduleFirstUpdate() {
    const runLoop = () => {
        updateLeaderboard();

        if (hasRealData) {
            updateInterval = setInterval(updateLeaderboard, UPDATE_FREQUENCY);
        } else {
            setTimeout(runLoop, FAST_POLL_INTERVAL);
        }
    };

    if ('requestIdleCallback' in window) {
        requestIdleCallback(runLoop, { timeout: 3000 });
    } else {
        setTimeout(runLoop, 2000);
    }
}

scheduleFirstUpdate();

});