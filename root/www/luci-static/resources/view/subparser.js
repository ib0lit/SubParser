'use strict';
'require view';
'require form';
'require fs';
'require ui';

return view.extend({
    serverLinks: [],
    dashboardSections: [],
    pingCache: {},
    availableSections: ['main'],
    githubBranches: ['main'],
    githubReleases: [],
    subscriptions: [],

    // Запрос helper с жёстким таймаутом на стороне браузера. Необязательные
    // данные (GitHub, подписки) не должны задерживать открытие страницы,
    // если сеть или GitHub недоступны: показываем форму с запасными
    // значениями, а не ждём curl.
    execJson: function(action, fallback, timeout) {
        var task = fs.exec('/usr/bin/subparser-helper.sh', [action]).then(function(res) {
            try {
                var parsed = JSON.parse((res.stdout || '').trim() || 'null');
                return parsed === null ? fallback : parsed;
            } catch(e) {
                return fallback;
            }
        }).catch(function() {
            return fallback;
        });
        if (!timeout) return task;
        return new Promise(function(resolve) {
            var settled = false;
            var timer = setTimeout(function() {
                if (settled) return;
                settled = true;
                resolve(fallback);
            }, timeout);
            task.then(function(value) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(value);
            });
        });
    },

    load: function() {
        var self = this;
        // Дашборд нужен для формы, а GitHub/подписки — только для значений
        // списков. Запускаем всё параллельно и ограничиваем необязательные
        // запросы, чтобы страница не висела на недоступной сети.
        var dashboardTask = this.execJson('get_dashboard', {}).then(function(data) {
            if (data && Array.isArray(data.sections) && data.sections.length)
                return data.sections;
            return self.loadLegacySections();
        });
        var branchesTask = this.execJson('get_github_branches', ['main'], 2500);
        var releasesTask = this.execJson('get_github_releases', [], 2500);
        var subsTask = this.execJson('get_subscriptions', [], 2500);

        return Promise.all([dashboardTask, branchesTask, releasesTask, subsTask]).then(function(results) {
            var sections = results[0] || [];
            self.dashboardSections = sections;
            self.availableSections = sections.map(function(sec) { return sec.name; });
            if (!self.availableSections.length) self.availableSections = ['main'];
            self.githubBranches = Array.isArray(results[1]) && results[1].length ? results[1] : ['main'];
            self.githubReleases = Array.isArray(results[2]) ? results[2] : [];
            self.subscriptions = Array.isArray(results[3]) ? results[3] : [];
        });
    },

    loadLegacySections: function() {
        var self = this;
        var empty = function(sec) {
            return { name: sec, links: [],
                selector: { tag: '', now: '', mode: 'unknown' },
                urltest: { enabled: false, tag: '', now: '' } };
        };
        return fs.exec('/usr/bin/subparser-helper.sh', ['get_podkop_sections']).then(function(res) {
            var sections = ['main'];
            try {
                var parsed = JSON.parse(res.stdout.trim() || '["main"]');
                if (Array.isArray(parsed) && parsed.length) sections = parsed;
            } catch(e) {}
            return Promise.all(sections.map(function(sec) {
                return fs.exec('/usr/bin/subparser-helper.sh', ['get_links', sec]).then(function(r) {
                    var item = empty(sec);
                    try { item.links = JSON.parse(r.stdout.trim() || '[]'); } catch(e) {}
                    return item;
                }).catch(function() { return empty(sec); });
            }));
        }).catch(function() { return [empty('main')]; });
    },

    handleSaveApply: function(ev, mode) {
        return this.super('handleSaveApply', [ev, mode]).then(function() {
            return fs.exec('/usr/bin/subparser-helper.sh', ['update_cron']);
        });
    },

    parseNodeInfo: function(link) {
        var proto = 'UNKNOWN', name = 'Без названия';
        try {
            var parts = link.split('://');
            proto = parts[0].toUpperCase();
            var rest = parts[1] || '';
            var hashIdx = rest.indexOf('#');
            name = (hashIdx !== -1) ? decodeURIComponent(rest.substring(hashIdx + 1)).trim() : (rest.split('@')[1] || rest);
        } catch(e) {}
        return { proto: proto, name: name, raw: link };
    },

    nodeNameForTag: function(secData, tag) {
        if (!tag) return '';
        var links = (secData && secData.links) || [];
        var m = /-(\d+)-out$/.exec(tag);
        if (!m) return '';
        var idx = parseInt(m[1], 10) - 1;
        if (idx < 0 || idx >= links.length) return '';
        return this.parseNodeInfo(links[idx]).name;
    },

    renderSectionBlock: function(secData) {
        var self = this;
        var sec = secData.name || 'main';
        var links = secData.links || [];
        var selector = secData.selector || {};
        var urltest = secData.urltest || {};
        var apiAvailable = secData.api !== false;

        var baseCardStyle = 'background: #1e1e1e; border-radius: 7px; padding: 9px 10px; ' +
            'display: flex; flex-direction: column; justify-content: space-between; color: #eee; ' +
            'box-shadow: 0 2px 4px rgba(0,0,0,0.2);';
        var normalBorder = ' border: 1px solid #333;';
        var activeBorder = ' border: 2px solid #00e676; box-shadow: 0 0 0 1px rgba(0,230,118,0.35), 0 2px 10px rgba(0,230,118,0.18);';

        // URLTest — отдельная карточка в начале секции. Она показывает сервер,
        // который выбрала группа URLTest, даже если селектор сейчас указывает
        // на конкретный узел вручную.
        var urlTitle, urlSubtitle, urlState;
        if (!apiAvailable) {
            urlTitle = _('Нет данных');
            urlSubtitle = _('Clash API Podkop недоступен');
            urlState = '—';
        } else if (!urltest.enabled) {
            urlTitle = _('Не включён');
            urlSubtitle = _('Группа URLTest отсутствует в конфигурации');
            urlState = _('выкл');
        } else if (urltest.now) {
            urlTitle = self.nodeNameForTag(secData, urltest.now) || urltest.now;
            urlSubtitle = _('Сервер, который сейчас выбирает URLTest');
            urlState = _('авто');
        } else {
            urlTitle = _('Не выбран');
            urlSubtitle = _('URLTest включён, но активный сервер не определён');
            urlState = _('авто');
        }

        var urltestSelected = selector.mode === 'auto' ||
            (selector.now && urltest.tag && selector.now === urltest.tag);

        var activateCurrent = function(ev, tag, allowed, reason) {
            if (ev.target && (ev.target.tagName === 'INPUT' || ev.target.tagName === 'BUTTON'))
                return;
            if (!allowed) {
                ui.addNotification(null, E('p', reason || _('Clash API Podkop недоступен — переключение невозможно.')), 'warning');
                return;
            }
            var block = ev.currentTarget.closest('.subparser-dashboard-section');
            if (block.getAttribute('data-active-tag') === tag) return;
            self.switchActiveNode(block, sec, tag, selector.tag || '');
        };

        var urlCard = E('div', {
            'id': 'card_' + (urltest.tag || (sec + '-urltest-out')),
            'class': 'subparser-urltest-card',
            'title': urltest.enabled ? _('Нажмите, чтобы включить авто-режим URLTest') : _('URLTest выключен'),
            'style': baseCardStyle + (urltestSelected ? activeBorder : normalBorder) +
                ' background: linear-gradient(135deg, #1b2620, #1e1e1e);' +
                (urltest.enabled && apiAvailable ? ' cursor: pointer;' : ''),
            'click': function(ev) {
                var reason = !apiAvailable
                    ? _('Clash API Podkop недоступен — переключение невозможно.')
                    : _('URLTest выключен.');
                activateCurrent(ev, urltest.tag,
                    apiAvailable && urltest.enabled && !!urltest.tag, reason);
            }
        }, [
            E('div', { 'style': 'display: flex; align-items: flex-start; justify-content: space-between; gap: 6px;' }, [
                E('div', { 'style': 'flex: 1; min-width: 0;' }, [
                    E('div', { 'style': 'font-weight: 700; font-size: 11px; color: #4fc3f7; text-transform: uppercase; letter-spacing: 0.5px;' }, 'URLTest'),
                    E('div', { 'data-urltest-title': '1', 'style': 'margin-top: 4px; font-weight: 600; font-size: 13px; line-height: 1.25; word-break: break-word;' }, urlTitle)
                ]),
                E('span', { 'data-urltest-state': '1', 'style': 'font-size: 10px; color: #888; white-space: nowrap; text-transform: uppercase;' }, urlState)
            ]),
            E('div', { 'data-urltest-sub': '1', 'style': 'margin-top: 10px; font-size: 11px; color: #999; line-height: 1.3;' }, urlSubtitle)
        ]);

        var grid = E('div', {
            'class': 'subparser-node-grid',
            'style': 'display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 9px; margin-top: 10px;'
        });

        grid.appendChild(urlCard);

        if (!links.length) {
            grid.appendChild(E('div', { 'style': 'padding: 12px; color: #888;' },
                _('В секции Podkop [' + sec + '] нет серверов.')));
        } else {
            links.forEach(function(link, idx) {
                var info = self.parseNodeInfo(link);
                var tag = sec + '-' + (idx + 1) + '-out';
                var selected = selector.now === tag;
                var cachedDelay = self.pingCache[tag];
                var delayInfo = cachedDelay !== undefined ? self.delayInfo(cachedDelay) : { text: '…', color: '#888' };
                grid.appendChild(E('div', {
                    'id': 'card_' + tag,
                    'title': _('Нажмите, чтобы сделать сервер активным'),
                    'style': baseCardStyle + (selected ? activeBorder : normalBorder) +
                        (apiAvailable ? ' cursor: pointer;' : ''),
                    'click': function(ev) {
                        activateCurrent(ev, tag, apiAvailable);
                    }
                }, [
                    E('div', { 'style': 'display: flex; align-items: flex-start; justify-content: space-between; gap: 6px;' }, [
                        E('div', { 'style': 'font-weight: 600; font-size: 12px; line-height: 1.25; word-break: break-word; flex: 1;' }, info.name),
                        E('input', { 'type': 'checkbox', 'checked': true, 'data-link': link,
                            'style': 'cursor: pointer; width: 16px; height: 16px; margin-top: 1px;' })
                    ]),
                    E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between; margin-top: 10px; font-size: 11px;' }, [
                        E('span', { 'style': 'color: #999; text-transform: uppercase; font-weight: 500;' }, info.proto),
                        E('span', { 'id': 'badge_' + tag, 'class': 'node-delay-badge', 'data-tag': tag,
                            'style': 'color: ' + delayInfo.color + '; font-weight: bold; font-family: monospace; font-size: 12px;' }, delayInfo.text)
                    ])
                ]));
            });
        }

        return E('div', {
            'class': 'cbi-section subparser-dashboard-section',
            'data-section': sec,
            'data-active-tag': selector.now || '',
            'style': 'margin-bottom: 18px; padding: 11px; border: 1px solid #333; border-radius: 9px; background: #181818;'
        }, [
            E('div', { 'style': 'display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px;' }, [
                E('h3', { 'style': 'margin: 0; font-size: 16px;' }, '📁 ' + sec)
            ]),
            E('div', { 'style': 'display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px;' }, [
                E('button', {
                    'class': 'btn cbi-button-action',
                    'click': function(ev) { ev.preventDefault(); self.refreshPings(ev.target.closest('.subparser-dashboard-section'), sec); }
                }, _('⚡ Пинги секции')),
                E('button', {
                    'class': 'btn cbi-button-positive',
                    'click': function(ev) { ev.preventDefault(); self.saveSectionNodes(ev.target.closest('.subparser-dashboard-section'), sec); }
                }, _('💾 Применить секцию')),
                E('button', {
                    'class': 'btn cbi-button-neutral',
                    'click': function(ev) {
                        ev.preventDefault();
                        var block = ev.target.closest('.subparser-dashboard-section');
                        var cbs = block.querySelectorAll('.subparser-node-grid input[type="checkbox"]');
                        var allChecked = Array.from(cbs).every(function(c) { return c.checked; });
                        cbs.forEach(function(c) { c.checked = !allChecked; });
                    }
                }, _('Выделить все / Снять'))
            ]),
            E('div', { 'class': 'subparser-ping-summary',
                'style': 'font-size: 12px; color: #888; margin-top: 8px;' }, ''),
            grid
        ]);
    },

    getPingUrl: function() {
        try {
            var saved = window.localStorage.getItem('subparser_ping_url');
            if (saved) return saved;
        } catch(e) {}
        return 'https://www.gstatic.com/generate_204';
    },

    setPingUrl: function(value) {
        var url = String(value || '').trim();
        if (url && !/^https?:\/\//i.test(url)) url = 'https://' + url;
        try {
            if (url) window.localStorage.setItem('subparser_ping_url', url);
            else window.localStorage.removeItem('subparser_ping_url');
        } catch(e) {}
        // Замеры привязаны к URL: старые значения больше не актуальны.
        this.clearPingCache();
        return url;
    },

    delayInfo: function(delay) {
        if (typeof delay === 'number' && delay > 0) {
            return {
                text: delay + ' ms',
                color: delay < 180 ? '#00e676' : (delay < 350 ? '#ffb300' : '#ff5252')
            };
        }
        return { text: 'timeout', color: '#ff5252' };
    },

    applyActiveCard: function(block, tag) {
        if (!block) return;
        block.setAttribute('data-active-tag', tag || '');
        Array.prototype.forEach.call(block.querySelectorAll('.subparser-node-grid > div[id]'), function(card) {
            var active = card.id === 'card_' + tag;
            card.style.border = active ? '2px solid #00e676' : '1px solid #333';
            card.style.boxShadow = active
                ? '0 0 0 1px rgba(0,230,118,0.35), 0 2px 10px rgba(0,230,118,0.18)'
                : '0 2px 4px rgba(0,0,0,0.2)';
        });
    },

    clearPingCache: function(sec) {
        var self = this;
        if (!sec) {
            this.pingCache = {};
            return;
        }
        var prefix = sec + '-';
        Object.keys(this.pingCache).forEach(function(key) {
            if (key.indexOf(prefix) === 0) delete self.pingCache[key];
        });
    },

    updatePingSummary: function(block, sec) {
        var self = this;
        var badges = Array.from(block.querySelectorAll('.node-delay-badge'));
        var summary = block.querySelector('.subparser-ping-summary');
        if (!summary) return;
        var ok = 0, failed = 0, unknown = 0;
        badges.forEach(function(b) {
            var value = self.pingCache[b.getAttribute('data-tag')];
            if (value === undefined) unknown++;
            else if (typeof value === 'number' && value > 0) ok++;
            else failed++;
        });
        if (!badges.length || unknown === badges.length) {
            summary.textContent = '';
            return;
        }
        summary.textContent = _('Секция ') + sec + ': ' + ok + _(' из ') + badges.length +
            _(' узлов отвечают') + (failed ? _(' (') + failed + _(' с таймаутом)') : '') +
            (unknown ? _(' (не измерено: ') + unknown + ')' : '');
    },

    scheduleAutoPings: function(container) {
        var self = this;
        if (this._autoPingTimer) clearInterval(this._autoPingTimer);
        var attempts = 0;
        this._autoPingTimer = setInterval(function() {
            attempts++;
            // Контейнер появляется в DOM сразу после render(). Если его так
            // и нет (страница уже заменена), прекращаем опрос через минуту.
            if (!document.body.contains(container)) {
                if (attempts > 150) {
                    clearInterval(self._autoPingTimer);
                    self._autoPingTimer = null;
                }
                return;
            }
            // Скрытая вкладка: ждём, пока пользователь её откроет. Ждать
            // можно сколько угодно — таймер один и ничего не запрашивает.
            if (container.offsetParent === null) return;
            clearInterval(self._autoPingTimer);
            self._autoPingTimer = null;
            // Тяжёлые карточки секций строим только когда вкладка «Дашборд»
            // реально открыта. Раньше это происходило при загрузке страницы.
            if (!self._dashboardRendered) {
                self.renderDashboard(container);
                self._dashboardRendered = true;
            }
            self.autoRefreshPings(container);
            self.startActiveRefresh(container);
        }, 400);
    },

    autoRefreshPings: function(container) {
        var self = this;
        var blocks = Array.from(container.querySelectorAll('.subparser-dashboard-section'));
        var queue = [];
        blocks.forEach(function(block) {
            var missing = false;
            Array.prototype.forEach.call(block.querySelectorAll('.node-delay-badge'), function(b) {
                if (self.pingCache[b.getAttribute('data-tag')] === undefined) missing = true;
            });
            if (missing) queue.push(block);
            else self.updatePingSummary(block, block.getAttribute('data-section'));
        });

        // Секции проверяем по две за раз, а не все одновременно: group-delay
        // внутри sing-box и так параллельный, но роутер не захлёбывается
        // десятком python-процессов, и результаты появляются быстро.
        var CONCURRENCY = 2;
        var index = 0;
        var runNext = function() {
            if (index >= queue.length) return;
            var block = queue[index++];
            var sec = block.getAttribute('data-section');
            self.refreshPings(block, sec).then(runNext);
        };
        for (var i = 0; i < Math.min(CONCURRENCY, queue.length); i++) runNext();
    },

    // Кнопка «Обновить пинги всех секций»: сбрасываем кэш и идём тем же
    // ограниченным путём, что и автообновление.
    refreshAllPings: function(container) {
        var self = this;
        Array.prototype.forEach.call(container.querySelectorAll('.subparser-dashboard-section'), function(block) {
            var sec = block.getAttribute('data-section');
            var prefix = sec + '-';
            Object.keys(self.pingCache).forEach(function(key) {
                if (key.indexOf(prefix) === 0) delete self.pingCache[key];
            });
            Array.prototype.forEach.call(block.querySelectorAll('.node-delay-badge'), function(b) {
                b.innerText = '...';
                b.style.color = '#888';
            });
            var summary = block.querySelector('.subparser-ping-summary');
            if (summary) summary.textContent = '';
        });
        self.autoRefreshPings(container);
    },

    applyActiveState: function(block, state) {
        var self = this;
        var sec = block.getAttribute('data-section');
        var secData = null;
        for (var i = 0; i < (this.dashboardSections || []).length; i++) {
            if (this.dashboardSections[i].name === sec) { secData = this.dashboardSections[i]; break; }
        }
        if (secData) {
            secData.api = state.api;
            if (state.selector) secData.selector = state.selector;
            if (state.urltest) secData.urltest = state.urltest;
        }
        var selector = (secData && secData.selector) || state.selector || {};
        var urltest = (secData && secData.urltest) || state.urltest || {};
        var apiAvailable = state.api !== false;

        var title, subtitle, stateText;
        if (!apiAvailable) {
            title = _('Нет данных');
            subtitle = _('Clash API Podkop недоступен');
            stateText = '—';
        } else if (!urltest.enabled) {
            title = _('Не включён');
            subtitle = _('Группа URLTest отсутствует в конфигурации');
            stateText = _('выкл');
        } else if (urltest.now) {
            title = self.nodeNameForTag(secData, urltest.now) || urltest.now;
            subtitle = _('Сервер, который сейчас выбирает URLTest');
            stateText = _('авто');
        } else {
            title = _('Не выбран');
            subtitle = _('URLTest включён, но активный сервер не определён');
            stateText = _('авто');
        }

        var titleEl = block.querySelector('[data-urltest-title]');
        var stateEl = block.querySelector('[data-urltest-state]');
        var subEl = block.querySelector('[data-urltest-sub]');
        if (titleEl) titleEl.textContent = title;
        if (stateEl) stateEl.textContent = stateText;
        if (subEl) subEl.textContent = subtitle;

        this.applyActiveCard(block, selector.now || '');
    },

    refreshActiveState: function(container) {
        var self = this;
        if (this._activeBusy) return Promise.resolve();
        this._activeBusy = true;
        return fs.exec('/usr/bin/subparser-helper.sh', ['get_active']).then(function(res) {
            var data = {};
            try { data = JSON.parse(res.stdout.trim() || '{}'); } catch(e) {}
            if (!data || !Array.isArray(data.sections)) return;
            var byName = {};
            data.sections.forEach(function(st) { byName[st.name] = st; });
            Array.prototype.forEach.call(
                container.querySelectorAll('.subparser-dashboard-section'),
                function(block) {
                    var state = byName[block.getAttribute('data-section')];
                    if (state) self.applyActiveState(block, state);
                });
        }).catch(function() {
            // Тихий опрос: при временной недоступности API просто ждём следующий.
        }).then(function() {
            self._activeBusy = false;
        });
    },

    startActiveRefresh: function(container) {
        var self = this;
        if (this._activeTimer) clearInterval(this._activeTimer);
        this._activeTimer = null;
        // URLTest переключается внутри sing-box сам: обновляем только
        // активные узлы, без повторных пингов и полной перерисовки.
        if (container.offsetParent !== null) self.refreshActiveState(container);
        this._activeTimer = setInterval(function() {
            if (!document.body.contains(container)) {
                clearInterval(self._activeTimer);
                self._activeTimer = null;
                return;
            }
            if (container.offsetParent === null) return;
            self.refreshActiveState(container);
        }, 15000);
    },

    renderDashboard: function(container) {
        var self = this;
        container.innerHTML = '';
        var sections = this.dashboardSections || [];
        if (!sections.length) {
            container.appendChild(E('div', { 'style': 'padding: 15px; color: #888;' },
                _('Не удалось получить список секций Podkop.')));
            return;
        }
        sections.forEach(function(secData) {
            container.appendChild(self.renderSectionBlock(secData));
        });
    },

    render: function(loadedLinks) {
        var self = this;
        this.serverLinks = loadedLinks || [];
        var m = new form.Map('subparser', _('SubParser'), _('Многопоточный парсер подписок с тонкой настройкой секций Podkop и дедупликацией.'));

        var s = m.section(form.NamedSection, 'settings', 'subparser', _('Настройки сервиса'));
        s.tab('general', _('⚙ Основные настройки'));
        s.tab('dashboard', _('📊 Дашборд'));
        s.tab('sections', _('📁 Секции Podkop'));
        s.tab('telegram', _('💬 Telegram'));
        s.tab('subscriptions', _('📋 Подписки'));
        s.tab('advanced', _('🛠 Дополнительно'));
        s.tab('watchdog', _('🛡 Watchdog'));
        s.tab('services', _('📡 Мониторинг сервисов'));

        var o = s.taboption('general', form.Flag, 'enabled', _('Включить сервис'));
        o.rmempty = false;

        o = s.taboption('general', form.ListValue, 'interval', _('Интервал автопроверки'));
        o.value('never', _('Никогда'));
        o.value('1h', _('1 час'));
        o.value('12h', _('12 часов'));
        o.value('24h', _('Раз в сутки (в полночь)'));
        o.value('custom_daily', _('Ежедневно в точное время (ЧЧ:ММ)'));
        o.value('custom_every_h', _('Каждые N часов в указанную минуту'));
        o.default = 'never';

        var ch = s.taboption('general', form.Value, 'custom_hour', _('Час (0-23)'));
        ch.datatype = 'range(0, 23)';
        ch.placeholder = '3';
        ch.default = '3';
        ch.depends('interval', 'custom_daily');
        ch.depends('interval', 'custom_every_h');

        var cm = s.taboption('general', form.Value, 'custom_minute', _('Минута (0-59)'));
        cm.datatype = 'range(0, 59)';
        cm.placeholder = '0';
        cm.default = '0';
        cm.depends('interval', 'custom_daily');
        cm.depends('interval', 'custom_every_h');

        o = s.taboption('general', form.Value, 'ping_threshold', _('Порог задержки (мс)'));
        o.datatype = 'uinteger';
        o.default = '350';

        o = s.taboption('general', form.Value, 'ping_threads', _('Потоков пинга'));
        o.datatype = 'range(1, 64)';
        o.default = '12';
        o.description = _('Сколько серверов проверять параллельно. По умолчанию 12. На слабых роутерах рекомендуется 6 — это снизит потребление оперативной памяти.');

        o = s.taboption('general', form.ListValue, 'max_best_nodes', _('Лимит лучших узлов'));
        o.value('0', _('Неограниченно'));
        o.value('3', _('Топ-3'));
        o.value('5', _('Топ-5'));
        o.value('10', _('Топ-10'));
        o.value('custom', _('Свой лимит'));
        o.default = '0';
        o.description = _('Сколько лучших узлов записывать. В режиме замены это общий размер списка, в режиме добавления — число новых узлов. При индивидуальной настройке секций значение можно переопределить для каждой секции отдельно.');

        o = s.taboption('general', form.Value, 'max_best_custom', _('Свой лимит (число)'));
        o.datatype = 'uinteger';
        o.placeholder = '20';
        o.default = '0';
        o.depends('max_best_nodes', 'custom');
        o.description = _('Используется, если выбран «Свой лимит». 0 — без ограничения.');

        o = s.taboption('general', form.Flag, 'filter_ru', _('Фильтровать RU / обход узлы'));
        o.default = '1';

        var excludedProtos = s.taboption('general', form.DynamicList, 'exclude_protocols', _('Исключаемые протоколы'));
        excludedProtos.description = _('Узлы выбранных протоколов отбрасываются сразу после загрузки подписки — до дедупликации и первого этапа скрининга.');
        excludedProtos.value('vless', 'VLESS');
        excludedProtos.value('trojan', 'Trojan');
        excludedProtos.value('hysteria2', 'Hysteria2 (hy2)');
        excludedProtos.value('ss', 'Shadowsocks (ss)');
        excludedProtos.value('socks4', 'SOCKS4');
        excludedProtos.value('socks4a', 'SOCKS4a');
        excludedProtos.value('socks5', 'SOCKS5');
        excludedProtos.default = [];

        var fpHint = s.taboption('general', form.DummyValue, '_fp_hint');
        fpHint.rawhtml = true;
        fpHint.render = function() {
            return E('details', {
                'class': 'alert-message info',
                'style': 'margin-top: 15px; margin-bottom: 12px; font-size: 13px; cursor: pointer;'
            }, [
                E('summary', { 'style': 'font-weight: bold; outline: none;' }, _('💡 uTLS Fingerprint: экстренная маскировка (нажмите, чтобы раскрыть)')),
                E('div', { 'style': 'margin: 10px 0 0 10px; line-height: 1.5; cursor: default;' }, [
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('<b>Что это такое:</b> Настройка глобально подменяет цифровой отпечаток браузера (TLS ClientHello) для узлов VLESS Reality/TLS и Trojan при проверке и выгрузке в Podkop.')),
                    E('p', { 'style': 'margin: 0 0 8px 0; color: #ffb300;' }, _('⚠️ <b>Важно:</b> Это экстренный инструмент. В обычных условиях держите значение <code>Как в подписке</code>.')),
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('<b>Когда изменять:</b> Меняйте эту опцию <u>только если РКН / ТСПУ начинает блокировать соединения по отпечаткам браузера</u> (массовые таймауты или сбросы рукопожатий на рабочих узлах).')),
                    E('p', { 'style': 'margin: 0;' }, _('<b>Рекомендации:</b> При блокировках переключайтесь на <b>edge</b> или <b>safari</b> — они построены на совместимых TLS-стеках, успешно обходят фильтры ТСПУ и не вызывают сбросов TCP на серверах Reality.'))
                ])
            ]);
        };

        var fpOpt = s.taboption('general', form.ListValue, 'override_fp', _('uTLS Fingerprint (эмуляция браузера)'));
        fpOpt.value('as_is', _('Как в подписке (по умолчанию: edge)'));
        fpOpt.value('edge', 'Edge (Chromium / Рекомендуется)');
        fpOpt.value('qq', 'QQ Browser (Chromium / 🐧)');
        fpOpt.value('safari', 'Safari (Apple WebKit / Рекомендуется)');
        fpOpt.value('chrome', 'Chrome (Google Chromium)');
        fpOpt.value('firefox', 'Firefox (Mozilla / РФ-маскировка)');
        fpOpt.value('ios', 'iOS (iPhone / iPad)');
        fpOpt.value('randomized', 'Randomized (Рандомизация TLS)');
        fpOpt.default = 'as_is';
        fpOpt.description = _('Глобальная подмена TLS-отпечатка. В штатном режиме рекомендуется <b>Как в подписке</b>.');

        o = s.taboption('general', form.Button, '_run_now', _('Синхронизация'));
        o.inputtitle = _('Синхронизировать сейчас');
        o.inputstyle = 'apply';
        o.onclick = function() {
            ui.showModal(_('Синхронизация'), [
                E('p', { 'class': 'spinning' }, _('Тестирование и отбор узлов...')),
                E('pre', {
                    'id': 'sync_live_log',
                    'style': 'max-height: 180px; overflow-y: auto; background: #111; color: #4af626; font-size: 11px; padding: 8px; border-radius: 4px; margin-top: 10px; text-align: left;'
                }, 'Инициализация синхронизации...')
            ]);

            return fs.exec('/usr/bin/subparser-helper.sh', ['bg_sync']).then(function() {
                var poll = setInterval(function() {
                    fs.read_direct('/tmp/subparser_sync.log').then(function(logTxt) {
                        var el = document.getElementById('sync_live_log');
                        if (el && logTxt) {
                            el.textContent = logTxt;
                            el.scrollTop = el.scrollHeight;
                        }
                    });

                    fs.exec('/usr/bin/subparser-helper.sh', ['check_sync']).then(function(cRes) {
                        var st = (cRes.stdout || '').trim();
                        if (st.indexOf('"done"') !== -1) {
                            clearInterval(poll);
                            setTimeout(function() {
                                ui.hideModal();
                                var root = document.getElementById('subparser_dashboard_box');
                                if (root) self.reloadDashboard(root);
                                ui.addNotification(null, E('p', _('Синхронизация завершена. Список узлов обновлён.')), 'info');
                            }, 800);
                        } else if (st.indexOf('"error"') !== -1) {
                            clearInterval(poll);
                            ui.hideModal();
                            ui.addNotification(null, E('p', _('Синхронизация завершилась с ошибкой. Проверьте лог.')), 'error');
                        }
                    });
                }, 1500);
            }).catch(function(err) {
                ui.hideModal();
                ui.addNotification(null, E('p', _('Ошибка вызова: ') + err.message), 'error');
            });
        };

        var per_sec = s.taboption('sections', form.Flag, 'per_section_config', _('Индивидуальная настройка каждой секции'));
        per_sec.description = _('Позволяет задать персональный режим обновления для каждого профиля Podkop.');
        per_sec.default = '0';

        var glob_sec = s.taboption('sections', form.DynamicList, 'target_section', _('Целевые секции Podkop'));
        glob_sec.description = _('Выберите секции для обновления.');
        self.availableSections.forEach(function(sec) { glob_sec.value(sec, sec); });
        glob_sec.default = self.availableSections;
        glob_sec.depends('per_section_config', '0');
        glob_sec.retain = true;

        var glob_mode = s.taboption('sections', form.ListValue, 'update_mode', _('Режим обновления'));
        glob_mode.value('replace', _('Заменять все серверы'));
        glob_mode.value('append', _('Добавлять к существующим (без дубликатов)'));
        glob_mode.default = 'replace';
        glob_mode.depends('per_section_config', '0');
        glob_mode.retain = true;

        var globPrune = s.taboption('sections', form.Flag, 'prune_dead_append', _('Удалять мёртвые узлы при добавлении'));
        globPrune.default = '0';
        globPrune.depends('per_section_config', '0');
        globPrune.retain = true;
        globPrune.description = _('В режиме добавления парсер дополнительно проверит уже установленные узлы секции и исключит те, что не отвечают. В режиме замены список и так перезаписывается.');

        self.availableSections.forEach(function(sec) {
            var sec_en = s.taboption('sections', form.Flag, 'sec_en_' + sec, _('Обновлять секцию') + ' [' + sec + ']');
            sec_en.default = '1';
            sec_en.depends('per_section_config', '1');
            sec_en.retain = true;

            var sec_m = s.taboption('sections', form.ListValue, 'sec_mode_' + sec, _('Режим для') + ' [' + sec + ']');
            sec_m.value('replace', _('Заменять все серверы'));
            sec_m.value('append', _('Добавлять к существующим (без дубликатов)'));
            sec_m.default = 'replace';
            sec_m.depends('per_section_config', '1');
            sec_m.retain = true;

            var secLimit = s.taboption('sections', form.ListValue, 'sec_limit_' + sec, _('Лимит узлов') + ' [' + sec + ']');
            secLimit.value('inherit', _('Как в глобальных настройках'));
            secLimit.value('0', _('Неограниченно'));
            secLimit.value('3', _('Топ-3'));
            secLimit.value('5', _('Топ-5'));
            secLimit.value('10', _('Топ-10'));
            secLimit.value('custom', _('Свой лимит'));
            secLimit.default = 'inherit';
            secLimit.depends('per_section_config', '1');
            secLimit.retain = true;

            var secLimitCustom = s.taboption('sections', form.Value, 'sec_limit_custom_' + sec, _('Свой лимит (число)') + ' [' + sec + ']');
            secLimitCustom.datatype = 'uinteger';
            secLimitCustom.placeholder = '20';
            secLimitCustom.default = '0';
            var customDeps = { per_section_config: '1' };
            customDeps['sec_limit_' + sec] = 'custom';
            secLimitCustom.depends(customDeps);
            secLimitCustom.retain = true;

            var secPrune = s.taboption('sections', form.ListValue, 'sec_prune_dead_' + sec, _('Удалять мёртвые при добавлении') + ' [' + sec + ']');
            secPrune.value('inherit', _('Как в глобальных настройках'));
            secPrune.value('1', _('Да'));
            secPrune.value('0', _('Нет'));
            secPrune.default = 'inherit';
            secPrune.depends('per_section_config', '1');
            secPrune.retain = true;

            if (self.subscriptions.length) {
                var secSubs = s.taboption('sections', form.MultiValue, 'sec_subs_' + sec,
                    _('Подписки-источники для') + ' [' + sec + ']');
                self.subscriptions.forEach(function(sub) {
                    var label = sub.name || sub.sec_name;
                    if (String(sub.enabled) !== '1')
                        label += ' (' + _('выкл') + ')';
                    secSubs.value(sub.sec_name, label);
                });
                secSubs.default = [];
                secSubs.depends('per_section_config', '1');
                secSubs.retain = true;
                secSubs.description = _('Из каких подписок брать новые узлы для этой секции. Ничего не выбрано — все активные подписки. В режиме «Добавлять» уже установленные узлы сохраняются.');
            }
        });

        o = s.taboption('advanced', form.Value, 'stress_top', _('Сколько кандидатов проверять на стабильность'));
        o.datatype = 'uinteger';
        o.default = '40';
        o.description = _('Ограничивает второй этап тестирования. 0 — проверять всех кандидатов (медленнее и больше нагрузка на CPU).');

        o = s.taboption('advanced', form.Value, 'hysteresis_pct', _('Гистерезис выбора узлов (%)'));
        o.datatype = 'range(0, 50)';
        o.default = '10';
        o.description = _('Уже выбранные узлы получают бонус к оценке, чтобы состав не менялся из-за случайных колебаний задержки.');

        o = s.taboption('advanced', form.Flag, 'allow_unstable_fallback', _('Разрешить fallback на нестабильные узлы'));
        o.default = '0';
        o.description = _('Если стабильных узлов не нашлось, использовать живые, но нестабильные. По умолчанию выключено: лучше сохранить старый список, чем залить плохой.');

        o = s.taboption('advanced', form.Flag, 'allow_insecure_tls', _('Отключить проверку TLS-сертификатов'));
        o.default = '0';
        o.description = _('⚠️ Небезопасно. Включайте только если подписка использует самоподписанный сертификат.');

        o = s.taboption('advanced', form.Flag, 'allow_local_diag', _('Разрешить диагностику локальных адресов'));
        o.default = '0';
        o.description = _('Разрешает проверять 127.0.0.1 и LAN-адреса через диагностику маршрута. По умолчанию запрещено (защита от SSRF).');

        o = s.taboption('advanced', form.Value, 'podkop_api', _('Clash API Podkop (host:port или URL)'));
        o.placeholder = '127.0.0.1:9090';
        o.rmempty = true;
        o.description = _('Оставьте пустым для автоопределения по LAN IP. Пример: <code>http://192.168.1.1:9090</code>.');

        o = s.taboption('advanced', form.Value, 'podkop_api_secret', _('Clash API secret'));
        o.password = true;
        o.rmempty = true;
        o.description = _('Секрет Clash API, если он включён в Podkop.');

        o = s.taboption('advanced', form.Value, 'speedtest_proxy', _('Прокси для теста скорости'));
        o.placeholder = 'socks5h://127.0.0.1:4534';
        o.default = 'socks5h://127.0.0.1:4534';
        o.rmempty = true;

        o = s.taboption('advanced', form.ListValue, 'update_branch', _('Ветка GitHub для обновлений'));
        var branches = (this.githubBranches && this.githubBranches.length)
            ? this.githubBranches : ['main'];
        if (branches.indexOf('main') === -1) branches.unshift('main');
        branches.forEach(function(name) {
            o.value(name, name === 'main' ? _('main (стабильная)') : name);
        });
        o.default = 'main';
        o.description = _('Список веток загружается из GitHub. Для стабильной работы выберите <code>main</code>.');

        o = s.taboption('advanced', form.ListValue, 'update_release', _('Версия GitHub для обновлений'));
        o.value('branch', _('Ветка из поля выше'));
        o.value('latest', _('Последний релиз'));
        var currentRelease = (this.data && this.data.update_release) ? this.data.update_release : 'branch';
        var seenReleases = {};
        (this.githubReleases || []).forEach(function(rel) {
            if (!rel || !rel.tag) return;
            seenReleases[rel.tag] = true;
            var label = rel.tag + (rel.prerelease ? _(' (beta)') : '');
            if (rel.name && rel.name !== rel.tag) label += ' — ' + rel.name;
            o.value(rel.tag, label);
        });
        if (currentRelease !== 'branch' && currentRelease !== 'latest' && !seenReleases[currentRelease]) {
            o.value(currentRelease, currentRelease);
        }
        o.default = 'branch';
        o.description = _('Выберите ветку, последний релиз или конкретный GitHub Release. Обновление запускается через Telegram-бота или скрипт <code>update.sh</code>; настройки и подписки сохраняются.');

        var wdHint = s.taboption('watchdog', form.DummyValue, '_watchdog_hint');
        wdHint.rawhtml = true;
        wdHint.render = function() {
            return E('details', {
                'class': 'alert-message info',
                'style': 'margin-bottom: 12px; font-size: 13px; cursor: pointer;'
            }, [
                E('summary', { 'style': 'font-weight: bold; outline: none;' }, _('💡 Как работает watchdog (нажмите, чтобы раскрыть)')),
                E('div', { 'style': 'margin: 10px 0 0 10px; line-height: 1.5; cursor: default;' }, [
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('Watchdog периодически проверяет процесс <b>sing-box</b> и доступность секций Podkop через Clash API. Если процесс отсутствует — перезапускает Podkop. Если секции не отвечают — после подтверждения запускает внеочередной отбор узлов.')),
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('Защита от ложных тревог: ожидание запуска ядра, grace-период после старта, проверка WAN, повторный замер группы и кулдаун между экстренными отборами.')),
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('Если задан список сервисов, watchdog дополнительно проверяет их через активный узел. При сбое он принудительно перепроверяет URLTest, а при повторном сбое может перебрать все узлы по адресу сервиса. Частота таких проверок растёт, когда через WAN идёт активный трафик, и снижается в простое.')),
                    E('p', { 'style': 'margin: 0; color: #ffb300;' }, _('⚠️ Значения по умолчанию соответствуют штатному поведению. Меняйте их только если понимаете последствия: слишком маленькие таймауты дают ложные срабатывания, слишком большие — замедляют реакцию.'))
                ])
            ]);
        };

        o = s.taboption('watchdog', form.Flag, 'watchdog_enabled', _('Включить watchdog'));
        o.default = '1';
        o.rmempty = false;
        o.description = _('Полностью включает или выключает watchdog. При выключении скрипт не выполняет никаких проверок и не отправляет уведомлений, даже если сервис SubParser включён.');

        o = s.taboption('watchdog', form.Value, 'watchdog_interval', _('Интервал проверки (минут)'));
        o.datatype = 'range(1, 60)';
        o.default = '5';
        o.depends('watchdog_enabled', '1');
        o.description = _('Как часто проверять Podkop. Меньшие значения ускоряют реакцию, но чаще нагружают API и CPU. Значение 5 минут — как раньше.');

        o = s.taboption('watchdog', form.Flag, 'watchdog_sub_limits', _('Следить за лимитами подписок'));
        o.default = '1';
        o.rmempty = false;
        o.depends('watchdog_enabled', '1');
        o.description = _('Предупреждать в Telegram, если до окончания подписки осталось меньше 3 дней или израсходовано более 90% трафика. Не чаще одного раза в сутки на подписку.');

        o = s.taboption('watchdog', form.Flag, 'watchdog_wan_check', _('Не лечить Podkop при падении WAN'));
        o.default = '1';
        o.rmempty = false;
        o.depends('watchdog_enabled', '1');
        o.description = _('Перед перезапуском и экстренным отбором проверять интернет через независимые DNS. Если WAN недоступен, watchdog ничего не делает. Отключайте только если ping блокируется вашим провайдером.');

        o = s.taboption('watchdog', form.Value, 'watchdog_down_wait', _('Ожидание запуска sing-box (сек)'));
        o.datatype = 'range(2, 600)';
        o.default = '10';
        o.depends('watchdog_enabled', '1');
        o.description = _('Сколько ждать появления процесса sing-box, прежде чем считать, что Podkop упал. Проверка идёт каждые 2 секунды. Увеличьте на медленном роутере или при большом конфиге Podkop, чтобы не ловить момент перезапуска.');

        o = s.taboption('watchdog', form.Value, 'watchdog_restart_wait', _('Ожидание после перезапуска Podkop (сек)'));
        o.datatype = 'range(2, 600)';
        o.default = '15';
        o.depends('watchdog_enabled', '1');
        o.description = _('Сколько ждать подъёма sing-box после принудительного перезапуска Podkop. На слабых устройствах с большим числом секций и узлов имеет смысл увеличить до 60–120 секунд.');

        o = s.taboption('watchdog', form.Value, 'watchdog_young_sec', _('Grace-период после старта ядра (сек)'));
        o.datatype = 'range(0, 3600)';
        o.default = '35';
        o.depends('watchdog_enabled', '1');
        o.description = _('Не проверять секции, пока sing-box работает меньше указанного времени. Защищает от ложных срабатываний во время старта и перезапуска Podkop. Для больших конфигов рекомендуется 90–180 секунд.');

        o = s.taboption('watchdog', form.Value, 'watchdog_cooldown', _('Кулдаун между экстренными отборами (минут)'));
        o.datatype = 'range(1, 10080)';
        o.default = '45';
        o.depends('watchdog_enabled', '1');
        o.description = _('После первого зафиксированного сбоя watchdog ждёт указанное время, прежде чем подтвердить аварию и запустить внеочередной отбор. Защита от лишних запусков парсера. По умолчанию 45 минут.');

        o = s.taboption('watchdog', form.Value, 'watchdog_http_timeout', _('Таймаут запросов к Clash API (сек)'));
        o.datatype = 'range(2, 60)';
        o.default = '8';
        o.depends('watchdog_enabled', '1');
        o.description = _('Сколько ждать ответа Clash API при проверке задержки секции. На перегруженном роутере можно увеличить.');

        o = s.taboption('watchdog', form.Value, 'watchdog_test_timeout', _('Таймаут проверки узла (мс)'));
        o.datatype = 'range(500, 30000)';
        o.default = '4000';
        o.depends('watchdog_enabled', '1');
        o.description = _('Лимит на один тестовый запрос через выбранный узел. Слишком маленькое значение даёт ложные «сбои» на медленных, но живых узлах.');

        o = s.taboption('watchdog', form.Value, 'watchdog_group_timeout', _('Таймаут теста всей группы (мс)'));
        o.datatype = 'range(500, 30000)';
        o.default = '3000';
        o.depends('watchdog_enabled', '1');
        o.description = _('Лимит на принудительный тест urltest-группы, который выполняется перед повторным замером. Больше узлов в секции — больше времени нужно.');

        o = s.taboption('watchdog', form.Value, 'watchdog_group_retry', _('Пауза перед повторным замером (сек)'));
        o.datatype = 'range(0, 60)';
        o.default = '3';
        o.depends('watchdog_enabled', '1');
        o.description = _('Сколько ждать после принудительного теста группы, прежде чем повторить замер. Даёт urltest время переключиться на живой узел.');

        o = s.taboption('watchdog', form.Value, 'watchdog_test_url', _('URL для проверки секций'));
        o.placeholder = 'https://www.gstatic.com/generate_204';
        o.default = 'https://www.gstatic.com/generate_204';
        o.rmempty = true;
        o.depends('watchdog_enabled', '1');
        o.description = _('Адрес, который должен отвечать кодом 204. Смените, если ваши узлы блокируют Google или конкретный домен: иначе watchdog будет считать секции мёртвыми.');

        o = s.taboption('watchdog', form.Value, 'watchdog_max_run', _('Максимальное время проверки (сек)'));
        o.datatype = 'range(30, 3600)';
        o.default = '600';
        o.depends('watchdog_enabled', '1');
        o.description = _('Предохранитель для больших конфигураций: если проверка всех секций не уложилась в это время, она прерывается без выводов о сбое и без экстренного отбора.');

        o = s.taboption('watchdog', form.Flag, 'watchdog_restart_podkop', _('Перезапускать Podkop при падении sing-box'));
        o.default = '1';
        o.rmempty = false;
        o.depends('watchdog_enabled', '1');
        o.description = _('Если процесс sing-box пропал, перезапускать службу Podkop. При выключении watchdog только пришлёт уведомление.');

        o = s.taboption('watchdog', form.Flag, 'watchdog_emergency', _('Запускать экстренный отбор при подтверждённом сбое'));
        o.default = '1';
        o.rmempty = false;
        o.depends('watchdog_enabled', '1');
        o.description = _('После подтверждённой аварии запускать subparser.py --force для внеочередного отбора узлов. При выключении watchdog ограничится уведомлением.');

        o = s.taboption('watchdog', form.Flag, 'watchdog_notify_recovery', _('Сообщать о восстановлении'));
        o.default = '1';
        o.rmempty = false;
        o.depends('watchdog_enabled', '1');
        o.description = _('Отправлять в Telegram сообщения «Podkop восстановлен» и «Проксирование Podkop восстановлено». Уведомления о сбоях остаются включёнными.');

        // --- Мониторинг сервисов -------------------------------------------------
        var servicePresets = [
            { id: 'meta', label: 'Instagram', hint: 'сайт instagram.com и API комментариев', all: true },
            { id: 'telegram', label: 'Telegram', hint: 't.me, web.telegram.org' },
            { id: 'youtube', label: 'YouTube', hint: 'youtube.com, youtu.be' },
            { id: 'steam', label: 'Steam', hint: 'steampowered.com, steamcommunity.com' },
            { id: 'discord', label: 'Discord', hint: 'discord.com, discord.gg' },
            { id: 'twitch', label: 'Twitch', hint: 'twitch.tv, gql.twitch.tv' },
            { id: 'tiktok', label: 'TikTok', hint: 'tiktok.com, tiktokv.com' }
        ];

        var svcHint = s.taboption('services', form.DummyValue, '_service_hint');
        svcHint.rawhtml = true;
        svcHint.render = function() {
            return E('details', {
                'class': 'alert-message info',
                'style': 'margin-bottom: 12px; font-size: 13px; cursor: pointer;'
            }, [
                E('summary', { 'style': 'font-weight: bold; outline: none;' }, _('💡 Как работает мониторинг сервисов (нажмите, чтобы раскрыть)')),
                E('div', { 'style': 'margin: 10px 0 0 10px; line-height: 1.5; cursor: default;' }, [
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('Проверки выполняются только при активном трафике по WAN. Если трафик стоит, запросы к сервисам не отправляются.')),
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('Каждый сервис проверяется по нескольким HTTPS-адресам. Обычно достаточно ответа хотя бы одного, но Instagram считается доступным только когда отвечают оба адреса — сайт и API комментариев.')),
                    E('p', { 'style': 'margin: 0 0 8px 0;' }, _('Сбой подтверждается после нескольких неудач подряд. Затем URLTest принудительно перепроверяется, а при ручном выборе монитор может переключить селектор на лучший узел по адресу сервиса.')),
                    E('p', { 'style': 'margin: 0; color: #ffb300;' }, _('Используйте только https:// — Clash API игнорирует HTTP-адреса. Проверка идёт HEAD-запросом через активный узел, поэтому любой HTTP-ответ считается успехом.'))
                ])
            ]);
        };

        o = s.taboption('services', form.Flag, 'watchdog_service_enabled', _('Включить мониторинг сервисов'));
        o.default = '1';
        o.rmempty = false;
        o.description = _('Отдельный монитор доступности сервисов. Работает независимо от watchdog секций и запускается службой SubParser.');

        servicePresets.forEach(function(p) {
            var f = s.taboption('services', form.Flag, 'watchdog_service_' + p.id, _(p.label));
            f.default = '0';
            f.rmempty = false;
            f.depends('watchdog_service_enabled', '1');
            f.description = _('Проверяемые адреса: ') + p.hint +
                (p.all ? ' ' + _('Сервис считается доступным только если отвечают оба адреса.') : '');

            var secSel = s.taboption('services', form.MultiValue,
                'watchdog_service_sections_' + p.id,
                _('Секции для ') + p.label);
            self.availableSections.forEach(function(sec) {
                secSel.value(sec, sec);
            });
            var secDeps = { watchdog_service_enabled: '1' };
            secDeps['watchdog_service_' + p.id] = '1';
            secSel.depends(secDeps);
            secSel.description = _('Пусто — проверять во всех управляемых секциях Podkop. Выберите только те секции, где этот сервис должен контролироваться.');
        });

        var svcManual = s.taboption('services', form.DynamicList, 'watchdog_services', _('Свои адреса для проверки'));
        svcManual.placeholder = 'Instagram|sections=gemini,main|https://www.instagram.com/';
        svcManual.depends('watchdog_service_enabled', '1');
        svcManual.description = _('Формат: <code>Название|sections=секция1,секция2|https://адрес1|https://адрес2</code> или просто <code>https://адрес</code>. Блок <code>sections=</code> необязателен: без него проверяются все секции. Несколько адресов одного сервиса проверяются вместе. Только HTTPS.');

        o = s.taboption('services', form.Value, 'watchdog_service_active_interval', _('Интервал проверки при активном трафике (сек)'));
        o.datatype = 'range(10, 300)';
        o.default = '30';
        o.depends('watchdog_service_enabled', '1');
        o.description = _('Как часто проверять сервисы, пока по WAN идёт трафик. По умолчанию каждые 30 секунд. Меньше 10 секунд ставить не стоит: проверки начнут мешать друг другу.');

        o = s.taboption('services', form.Value, 'watchdog_service_traffic_kb', _('Порог активного трафика (КБ/с)'));
        o.datatype = 'range(1, 100000)';
        o.default = '20';
        o.depends('watchdog_service_enabled', '1');
        o.description = _('Средняя скорость по WAN (rx+tx), выше которой трафик считается активным. После остановки трафика мониторинг продолжается ещё 30 секунд, чтобы не пропустить хвост загрузки.');

        o = s.taboption('services', form.Value, 'watchdog_service_timeout', _('Таймаут проверки сервиса (мс)'));
        o.datatype = 'range(1000, 30000)';
        o.default = '8000';
        o.depends('watchdog_service_enabled', '1');
        o.description = _('Лимит на один HTTPS-запрос через узел. Для загруженных или мобильных узлов 8–10 секунд уменьшает ложные срабатывания.');

        o = s.taboption('services', form.Value, 'watchdog_service_fail_threshold', _('Неудач подряд для срабатывания'));
        o.datatype = 'range(1, 5)';
        o.default = '2';
        o.depends('watchdog_service_enabled', '1');
        o.description = _('Сколько проверок подряд должны завершиться ошибкой, прежде чем считать сервис недоступным. Значение 2 защищает от одиночных таймаутов.');

        o = s.taboption('services', form.Value, 'watchdog_service_cooldown', _('Кулдаун между уведомлениями (минут)'));
        o.datatype = 'range(1, 1440)';
        o.default = '30';
        o.depends('watchdog_service_enabled', '1');
        o.description = _('В течение этого времени новые события по тому же сервису не отправляются отдельным сообщением, а обновляют предыдущее. После окончания окна бот пришлёт новое сообщение. Переключения узлов не ограничиваются.');

        o = s.taboption('services', form.Flag, 'watchdog_service_manual_switch', _('Переключать узел при ручном выборе'));
        o.default = '1';
        o.rmempty = false;
        o.depends('watchdog_service_enabled', '1');
        o.description = _('Если секция вручную закреплена за узлом и сервис перестал открываться, проверить все узлы по адресу сервиса и переключить селектор на лучший. При выключении монитор только уведомит.');

        o = s.taboption('services', form.Flag, 'watchdog_service_notify', _('Уведомлять о сбоях и переключениях'));
        o.default = '1';
        o.rmempty = false;
        o.depends('watchdog_service_enabled', '1');
        o.description = _('Отправлять в Telegram сообщения о подтверждённом сбое и переключении узла. Уведомления о восстановлении сервиса не отправляются.');

        o = s.taboption('services', form.Button, '_service_check', _('Диагностика'));
        o.inputtitle = _('Проверить сервисы сейчас');
        o.inputstyle = 'action';
        o.depends('watchdog_service_enabled', '1');
        o.onclick = function(ev) {
            var btn = ev.target;
            btn.disabled = true;
            btn.innerText = _('Проверка...');
            return fs.exec('/usr/bin/subparser-service-monitor.py', ['--check']).then(function(res) {
                btn.disabled = false;
                btn.innerText = _('Проверить сервисы сейчас');
                var out = (res.stdout || '').trim() || (res.stderr || '').trim() || _('Нет вывода');
                ui.showModal(_('Диагностика сервисов'), [
                    E('pre', {
                        'style': 'max-height: 420px; overflow: auto; white-space: pre-wrap; font-size: 12px;'
                    }, out),
                    E('div', { 'class': 'right' }, [
                        E('button', {
                            'class': 'btn cbi-button',
                            'click': ui.hideModal
                        }, _('Закрыть'))
                    ])
                ]);
            });
        };

        o = s.taboption('telegram', form.Value, 'tg_bot_token', _('Telegram Bot Token'));
        o.placeholder = '123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ';
        o.rmempty = true;
        o.password = true;
        o.description = _('Токен бота от <b>@BotFather</b>.<br>' +
                          '<i>Как получить:</i> отправьте боту <code>@BotFather</code> команду <code>/newbot</code>, ' +
                          'укажите имя и юзернейм бота. Скопируйте полученный токен API.');

        o = s.taboption('telegram', form.Value, 'tg_chat_id', _('Основной Chat / User ID (Admin)'));
        o.placeholder = '123456789';
        o.rmempty = true;
        o.datatype = 'integer';
        o.description = _('Ваш цифровой ID администратора для получения системных алертов и отчетов сбоев.<br>' +
                          '<i>Как узнать:</i> напишите в Telegram боту <code>@userinfobot</code> или <code>@getmyid_bot</code> — скопируйте значение <b>Id</b>.');

        o = s.taboption('telegram', form.DynamicList, 'allowed_users', _('Авторизованные пользователи'));
        o.datatype = 'integer';
        o.description = _('Список дополнительных Telegram ID, которым разрешено нажимать кнопки и управлять ботом.<br>' +
                          'Укажите ID членов семьи или доверенных лиц, предварительно узнав их через <code>@userinfobot</code>.');
        o.rmempty = true;

        o = s.taboption('telegram', form.Flag, 'tg_rich_messages', _('Rich-сообщения в боте'));
        o.default = '1';
        o.rmempty = false;
        o.description = _('Оформлять меню бота таблицами, заголовками и раскрывающимися блоками (Telegram Rich Messages). ' +
                          'Логи и статусы становятся компактнее, а длинные сообщения больше не разбиваются на части. ' +
                          'Если клиент или Bot API не поддерживает rich-разметку, бот автоматически отправит обычный HTML.');

        o = s.taboption('telegram', form.Button, '_test_tg', _('Проверка связи'));
        o.inputtitle = _('Отправить тест в Telegram');
        o.inputstyle = 'action';
        o.onclick = function(ev) {
            var btn = ev.target;
            btn.disabled = true;
            btn.innerText = _('Отправка...');
            return fs.exec('/usr/bin/subparser-helper.sh', ['test_tg']).then(function(res) {
                btn.disabled = false;
                btn.innerText = _('Отправить тест в Telegram');
                var data = {};
                try { data = JSON.parse(res.stdout.trim()); } catch(e) {}
                if (data.status === 'ok') {
                    ui.addNotification(null, E('p', {}, _('Тестовое уведомление успешно отправлено!')), 'info');
                } else {
                    ui.addNotification(null, E('p', {}, _('Ошибка: ') + (data.message || 'Сбой сети')), 'danger');
                }
            });
        };

        var s_subs = m.section(form.GridSection, 'subscription', _('Источники подписок'));
        s_subs.tab = 'subscriptions';
        s_subs.addremove = true;
        s_subs.anonymous = true;

        var sub_en = s_subs.option(form.Flag, 'enabled', _('Активна'));
        sub_en.default = '1';
        sub_en.editable = true;

        var sub_name = s_subs.option(form.Value, 'name', _('Название'));
        sub_name.placeholder = 'Провайдер 1';

        var sub_url = s_subs.option(form.Value, 'url', _('URL подписки'));
        sub_url.placeholder = 'https://...';
        sub_url.rmempty = false;

        var sub_ua = s_subs.option(form.ListValue, 'user_agent', _('User-Agent'));
        sub_ua.value('v2rayN', 'v2rayN/6.42');
        sub_ua.value('flclash', 'FlClashX / ClashMeta');
        sub_ua.value('incy', 'Incy (Android)');
        sub_ua.value('singbox', 'Sing-box');
        sub_ua.value('happ', 'Happ (iOS)');
        sub_ua.value('shadowrocket', 'Shadowrocket');
        sub_ua.value('clashmeta', 'Clash.Meta (Mihomo)');
        sub_ua.value('clash', 'Clash for Windows');
        sub_ua.value('curl', 'curl (Generic)');
        sub_ua.value('custom', _('🛠 Свой (Эмуляция устройства)'));
        sub_ua.default = 'v2rayN';

        var subHint = s_subs.option(form.DummyValue, '_custom_hint');
        subHint.rawhtml = true;
        subHint.modalonly = true;
        subHint.depends('user_agent', 'custom');
        subHint.render = function() {
            return E('details', {
                'class': 'alert-message info',
                'style': 'margin-bottom: 12px; font-size: 13px; cursor: pointer;'
            }, [
                E('summary', { 'style': 'font-weight: bold; outline: none;' }, _('💡 Эмуляция своего устройства (экономия слотов подписки)')),
                E('div', { 'style': 'margin: 8px 0 0 10px; line-height: 1.5; cursor: default;' }, [
                    E('p', { 'style': 'margin: 0 0 6px 0;' }, _('Многие провайдеры подписок ограничивают число одновременно подключенных устройств. Если ваш сервис отображает в личном кабинете привязанные устройства (HWID, модель, клиент), вы можете попробовать скопировать эти данные сюда.')),
                    E('p', { 'style': 'margin: 0 0 6px 0;' }, _('В этом случае роутер будет обращаться к серверу от имени уже привязанного смартфона или ПК, возможно не расходуя отдельный слот.')),
                    E('p', { 'style': 'margin: 0; color: #ffb300;' }, _('⚠️ <b>Обратите внимание:</b> Данный способ не дает 100% гарантии. Некоторые панели отслеживают одновременные сессии с разных IP-адресов или используют закрытые токены авторизации.'))
                ])
            ]);
        };

        var sub_cua = s_subs.option(form.Value, 'custom_ua', _('Custom User-Agent'));
        sub_cua.placeholder = 'AppClient/1.0.0 (Android)';
        sub_cua.depends('user_agent', 'custom');
        sub_cua.modalonly = true;
        sub_cua.rmempty = true;

        var sub_hwid = s_subs.option(form.Value, 'device_id', _('Device ID / HWID'));
        sub_hwid.placeholder = 'a1b2c3d4e5f67890';
        sub_hwid.depends('user_agent', 'custom');
        sub_hwid.modalonly = true;
        sub_hwid.rmempty = true;

        var sub_model = s_subs.option(form.Value, 'device_model', _('Модель устройства'));
        sub_model.placeholder = 'Phone-Model-Name';
        sub_model.depends('user_agent', 'custom');
        sub_model.modalonly = true;
        sub_model.rmempty = true;

        var sub_ver = s_subs.option(form.Value, 'os_version', _('Версия ОС'));
        sub_ver.placeholder = '14 / 15 / 16';
        sub_ver.depends('user_agent', 'custom');
        sub_ver.modalonly = true;
        sub_ver.rmempty = true;

        var dashboardContainer = E('div', { 'id': 'subparser_dashboard_box' });

        var hintBox = E('details', {
            'class': 'alert-message info',
            'style': 'margin-bottom: 12px; font-size: 13px; cursor: pointer;'
        }, [
            E('summary', { 'style': 'font-weight: bold; outline: none;' }, _('💡 Как работать с узлами (нажмите, чтобы раскрыть)')),
            E('ul', { 'style': 'margin: 8px 0 0 18px; padding: 0; line-height: 1.5; cursor: default;' }, [
                E('li', {}, _('На странице показаны все секции Podkop сразу.')),
                E('li', {}, _('Для каждой секции видно, какой сервер выбран в селекторе и какой сервер активен в URLTest (если он включён).')),
                E('li', {}, _('Карточка URLTest всегда стоит первой и показывает её текущий сервер. Выбор селектора выделен зелёной рамкой.')),
                E('li', {}, _('Клик по карточке узла сразу переключает на него селектор. Клик по карточке URLTest возвращает авто-режим.')),
                E('li', {}, _('Пинги измеряются автоматически при открытии дашборда; кнопка «⚡ Пинги секции» перепроверяет задержки вручную.')),
                E('li', {}, _('URL для проверки пингов можно изменить в поле над кнопками — он сохраняется в этом браузере.')),
                E('li', {}, _('Активный сервер URLTest обновляется автоматически раз в 15 секунд, пока открыта эта вкладка.')),
                E('li', {}, _('Чекбоксы и «💾 Применить» нужны для записи выбранного списка узлов в конфигурацию Podkop.')),
                E('li', {}, _('Снять все галочки в секции нельзя: Podkop не запустится без узлов. Активный сервер при сохранении сохраняется.')),
                E('li', {}, _('«⚡ Пинги секции» измеряет задержки серверов этой секции. «💾 Применить секцию» записывает только выбранные узлы этой секции.')),
                E('li', {}, _('Кнопки сверху обновляют данные и применяют выбор сразу во всех секциях.'))
            ])
        ]);

        var pingUrlInput = E('input', {
            'type': 'text',
            'class': 'cbi-input-text',
            'style': 'flex: 1 1 340px; min-width: 240px; font-family: monospace;',
            'placeholder': self.getPingUrl(),
            'value': self.getPingUrl(),
            'change': function(ev) { applyPingUrl(ev.target.value); }
        });

        // Свой URL проверки: сохраняется в браузере и передаётся в helper.
        var applyPingUrl = function(value) {
            var url = self.setPingUrl(value);
            pingUrlInput.value = url || self.getPingUrl();
            // Старые задержки измерены по другому адресу — сбрасываем кэш.
            if (self._dashboardRendered && dashboardContainer.offsetParent !== null)
                self.refreshAllPings(dashboardContainer);
        };

        var pingUrlRow = E('div', {
            'style': 'display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 10px;'
        }, [
            E('label', { 'style': 'font-weight: 600; font-size: 13px;' }, _('URL для проверки пингов:')),
            pingUrlInput,
            E('button', {
                'class': 'btn cbi-button-neutral',
                'click': function(ev) { ev.preventDefault(); applyPingUrl(''); }
            }, _('↺ По умолчанию'))
        ]);

        var toolbar = E('div', { 'style': 'display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 12px;' }, [
            E('button', {
                'class': 'btn cbi-button-neutral',
                'click': function(ev) {
                    ev.preventDefault();
                    self.reloadDashboard(dashboardContainer).then(function() {
                        ui.addNotification(null, E('p', _('Данные секций Podkop обновлены.')), 'info');
                    }).catch(function(err) {
                        ui.addNotification(null, E('p', _('Не удалось обновить данные: ') + (err.message || err)), 'danger');
                    });
                }
            }, _('🔄 Обновить данные')),
            E('button', {
                'class': 'btn cbi-button-action',
                'click': function(ev) {
                    ev.preventDefault();
                    self.refreshAllPings(dashboardContainer);
                }
            }, _('⚡ Обновить пинги всех секций')),
            E('button', {
                'class': 'btn cbi-button-positive',
                'click': function(ev) { ev.preventDefault(); self.saveAllSelected(dashboardContainer); }
            }, _('💾 Применить выбранные узлы')),
            E('button', {
                'class': 'btn cbi-button-neutral',
                'click': function(ev) {
                    ev.preventDefault();
                    var cbs = dashboardContainer.querySelectorAll('.subparser-node-grid input[type="checkbox"]');
                    var allChecked = Array.from(cbs).every(function(c) { return c.checked; });
                    cbs.forEach(function(c) { c.checked = !allChecked; });
                }
            }, _('Выделить все / Снять'))
        ]);

        var dummy = s.taboption('dashboard', form.DummyValue, '_dashboard');
        dummy.rawhtml = true;
        dummy.render = function() { return E('div', {}, [hintBox, pingUrlRow, toolbar, dashboardContainer]); };
        // Карточки секций строятся лениво, при первом открытии вкладки
        // «Дашборд» (см. scheduleAutoPings). Загрузка главной страницы
        // больше не ждёт пингов и не рисует скрытый дашборд.
        self._dashboardRendered = false;
        self.scheduleAutoPings(dashboardContainer);

        return m.render();
    },

    reloadDashboard: function(container) {
        var self = this;
        // Переключение активного узла не должно сбрасывать галочки, которые
        // пользователь уже расставил для применения списка.
        var unchecked = {};
        Array.prototype.forEach.call(
            container.querySelectorAll('.subparser-node-grid input[type="checkbox"][data-link]'),
            function(cb) {
                if (!cb.checked) unchecked[cb.getAttribute('data-link')] = true;
            });
        return fs.exec('/usr/bin/subparser-helper.sh', ['get_dashboard']).then(function(res) {
            var data = {};
            try { data = JSON.parse(res.stdout.trim() || '{}'); } catch(e) {}
            if (!data || !Array.isArray(data.sections) || !data.sections.length)
                throw new Error(_('Не удалось получить данные секций Podkop'));
            self.dashboardSections = data.sections;
            self.availableSections = data.sections.map(function(sec) { return sec.name; });
            self.renderDashboard(container);
            self._dashboardRendered = true;
            if (Object.keys(unchecked).length) {
                Array.prototype.forEach.call(
                    container.querySelectorAll('.subparser-node-grid input[type="checkbox"][data-link]'),
                    function(cb) {
                        if (unchecked[cb.getAttribute('data-link')]) cb.checked = false;
                    });
            }
            self.autoRefreshPings(container);
            return data.sections;
        });
    },

    refreshPings: function(container, sec) {
        var self = this;
        var s = sec || 'main';
        var pingUrl = self.getPingUrl();
        var badges = Array.from(container.querySelectorAll('.node-delay-badge'));
        badges.forEach(function(b) { b.innerText = '...'; b.style.color = '#888'; });
        var summary = container.querySelector('.subparser-ping-summary');
        if (summary) summary.textContent = _('Измерение задержек...');

        var setBadge = function(b, delay) {
            var info = self.delayInfo(delay);
            self.pingCache[b.getAttribute('data-tag')] = delay;
            b.innerText = info.text;
            b.style.color = info.color;
            return typeof delay === 'number' && delay > 0;
        };

        var resolveDone;
        var settled = false;
        var done = new Promise(function(resolve) { resolveDone = resolve; });

        var finish = function(okCount, failed) {
            if (summary) {
                summary.textContent = _('Секция ') + s + ': ' + okCount + _(' из ') + badges.length +
                    _(' узлов отвечают') + (failed ? _(' (') + failed + _(' с таймаутом)') : '');
            }
            if (!settled) {
                settled = true;
                resolveDone();
            }
        };

        // Разбирает ответ вида { "main-1-out": 123, ... } и возвращает
        // счётчики плюс бейджи, для которых ответа не было.
        var applyDelays = function(delays) {
            var state = { okCount: 0, failed: 0, missing: [] };
            badges.forEach(function(b) {
                var tag = b.getAttribute('data-tag');
                if (Object.prototype.hasOwnProperty.call(delays, tag)) {
                    if (setBadge(b, delays[tag])) state.okCount++; else state.failed++;
                } else {
                    b.innerText = '...';
                    state.missing.push(b);
                }
            });
            return state;
        };

        // Добор узлов, которых не было в общем ответе. Параллелизм ограничен,
        // чтобы роутер не захлёбывался, но 6 потоков заметно быстрее 4.
        var runMissing = function(state) {
            var index = 0;
            var CONCURRENCY = 6;
            if (!state.missing.length) { finish(state.okCount, state.failed); return; }
            var next = function() {
                if (index >= state.missing.length) {
                    finish(state.okCount, state.failed);
                    return;
                }
                var b = state.missing[index++];
                var tag = b.getAttribute('data-tag');
                fs.exec('/usr/bin/subparser-helper.sh', ['ping_node', tag, pingUrl]).then(function(nodeRes) {
                    var data = {};
                    try { data = JSON.parse(nodeRes.stdout.trim()); } catch(err) {}
                    if (setBadge(b, data.delay)) state.okCount++; else state.failed++;
                }).catch(function() {
                    setBadge(b, -1);
                    state.failed++;
                }).then(next);
            };
            for (var i = 0; i < Math.min(CONCURRENCY, state.missing.length); i++) next();
        };

        // Старый путь на случай, если helper ещё не обновлён и не знает
        // действие ping_section.
        var legacy = function() {
            fs.exec('/usr/bin/subparser-helper.sh', ['ping_group', s, pingUrl]).then(function(res) {
                var groupDelays = {};
                try { groupDelays = JSON.parse(res.stdout.trim() || '{}'); } catch(e) {}
                runMissing(applyDelays(groupDelays));
            }).catch(function() {
                runMissing({ okCount: 0, failed: 0, missing: badges.slice() });
            });
        };

        fs.exec('/usr/bin/subparser-helper.sh', ['ping_section', s, pingUrl]).then(function(res) {
            var delays = {};
            try { delays = JSON.parse(res.stdout.trim() || '{}'); } catch(e) {}
            var state = applyDelays(delays);
            if (badges.length && state.missing.length === badges.length) {
                legacy();
                return;
            }
            runMissing(state);
        }).catch(function() { legacy(); });
        return done;
    },

    switchActiveNode: function(block, sec, tag, selectorTag) {
        var self = this;
        if (this.switching) return;
        this.switching = true;

        // Сразу переносим зелёную рамку: не ждём ответа API, чтобы
        // переключение ощущалось мгновенным.
        var secData = null;
        for (var i = 0; i < (this.dashboardSections || []).length; i++) {
            if (this.dashboardSections[i].name === sec) { secData = this.dashboardSections[i]; break; }
        }
        var previousTag = block.getAttribute('data-active-tag') || '';
        var previousMode = secData ? secData.selector.mode : 'unknown';
        var nextMode = (secData && secData.urltest && secData.urltest.tag === tag) ? 'auto' : 'manual';
        if (secData) {
            secData.selector.now = tag;
            secData.selector.mode = nextMode;
        }
        this.applyActiveCard(block, tag);

        return fs.exec('/usr/bin/subparser-helper.sh', ['set_active', sec, tag, selectorTag || ''])
            .then(function(res) {
                var data = {};
                try { data = JSON.parse((res.stdout || '').trim()); } catch(e) {}
                if (!data || data.status !== 'ok')
                    throw new Error((data && data.message) || _('Не удалось переключить селектор'));
            }).catch(function(err) {
                // Возвращаем прежнюю подсветку, если Clash не принял переключение.
                if (secData) {
                    secData.selector.now = previousTag;
                    secData.selector.mode = previousMode;
                }
                self.applyActiveCard(block, previousTag);
                ui.addNotification(null, E('p', {}, _('Ошибка переключения: ') + (err.message || err)), 'danger');
            }).then(function() {
                self.switching = false;
            });
    },

    collectSelected: function(block) {
        var selectedLinks = [];
        block.querySelectorAll('.subparser-node-grid input[type="checkbox"]:checked').forEach(function(cb) {
            var link = cb.getAttribute('data-link');
            if (link) selectedLinks.push(link);
        });
        return selectedLinks;
    },

    dashboardRoot: function(block) {
        return block.closest('#subparser_dashboard_box') || block;
    },

    newSaveNonce: function() {
        return Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    },

    // Все сохранения выполняются строго по очереди. Раньше два нажатия
    // «Применить» могли перезаписать общий /tmp-файл и применить выбор
    // одной секции к другой.
    enqueueSave: function(task) {
        var run = (this._saveQueue || Promise.resolve()).then(task);
        this._saveQueue = run.then(function() {}, function() {});
        return run;
    },

    // Ждём маркер фонового перезапуска Podkop. Каждый опрос — отдельный
    // быстрый запрос, поэтому XHR не упирается в таймаут.
    waitForRestart: function(token) {
        var attempts = 0;
        var done = false;
        return new Promise(function(resolve) {
            var tick = function() {
                if (done) return;
                if (attempts++ >= 45) { done = true; resolve(); return; }
                fs.exec('/usr/bin/subparser-helper.sh', ['get_save_status', token])
                    .then(function(res) {
                        var data = {};
                        try { data = JSON.parse((res.stdout || '').trim()); } catch(e) {}
                        if (data && data.ready) { done = true; resolve(); return; }
                        setTimeout(tick, 1000);
                    }).catch(function() {
                        setTimeout(tick, 1000);
                    });
            };
            setTimeout(tick, 700);
        });
    },

    selectionForSection: function(sec) {
        var secData = null;
        for (var i = 0; i < (this.dashboardSections || []).length; i++) {
            if (this.dashboardSections[i].name === sec) { secData = this.dashboardSections[i]; break; }
        }
        if (!secData || !secData.selector) return null;
        var selector = secData.selector;
        if (selector.mode === 'auto') return { mode: 'auto', link: null };
        if (selector.mode !== 'manual' || !selector.now) return null;
        var prefix = sec + '-';
        var suffix = '-out';
        var tag = selector.now;
        if (tag.indexOf(prefix) !== 0 || tag.slice(-suffix.length) !== suffix)
            return null;
        var middle = tag.slice(prefix.length, -suffix.length);
        if (!/^\d+$/.test(middle)) return null;
        var index = parseInt(middle, 10) - 1;
        var links = secData.links || [];
        if (index < 0 || index >= links.length) return null;
        return { mode: 'manual', link: links[index] };
    },

    saveSectionNodes: function(block, sec) {
        var self = this;
        var s = sec || 'main';
        var selectedLinks = this.collectSelected(block);

        if (selectedLinks.length === 0) {
            ui.addNotification(null, E('p', {}, _('Выберите хотя бы один сервер в секции ') + s + '!'), 'danger');
            return;
        }

        ui.showModal(_('Сохранение'), [ E('p', { 'class': 'spinning' }, _('Запись выбранных узлов в секцию ' + s + ' (' + selectedLinks.length + ' шт.)...')) ]);

        var task = function() {
            var nonce = self.newSaveNonce();
            var payload = JSON.stringify({
                nonce: nonce,
                jobs: [{ section: s, links: selectedLinks,
                         selection: self.selectionForSection(s) }]
            });
            return fs.write('/tmp/subparser_selected.txt', payload).then(function() {
                return fs.exec('/usr/bin/subparser-helper.sh', ['save_selected', s, nonce]);
            }).then(function(res) {
                var data = {};
                try { data = JSON.parse((res.stdout || '').trim()); } catch(e) {}
                if (!data || data.status !== 'ok')
                    throw new Error((data && data.message) || _('Ошибка записи'));
            }).then(function() {
                return self.waitForRestart(nonce);
            });
        };

        this.enqueueSave(task).then(function() {
            ui.hideModal();
            ui.addNotification(null, E('p', {}, _('Секция ') + s + _(': сохранено узлов — ') + selectedLinks.length), 'info');
            self.clearPingCache(s);
            return self.reloadDashboard(self.dashboardRoot(block));
        }).catch(function(err) {
            ui.hideModal();
            ui.addNotification(null, E('p', {}, _('Ошибка сохранения: ') + (err.message || err)), 'danger');
        });
    },

    saveAllSelected: function(root) {
        var self = this;
        var blocks = root.querySelectorAll('.subparser-dashboard-section');
        var jobs = [];
        var empty = [];
        Array.prototype.forEach.call(blocks, function(block) {
            var sec = block.getAttribute('data-section');
            var boxes = block.querySelectorAll('.subparser-node-grid input[type="checkbox"][data-link]');
            if (!boxes.length) return; // в секции нет узлов — сохранять нечего
            var links = self.collectSelected(block);
            if (links.length) jobs.push({ sec: sec, links: links,
                                          selection: self.selectionForSection(sec) });
            else empty.push(sec);
        });

        // Раньше такая секция молча пропускалась, и её старые узлы
        // оставались в Podkop — выглядело как «удалённые серверы вернулись».
        if (empty.length) {
            ui.addNotification(null, E('p', {},
                _('Нельзя удалить все серверы в секции: ') + empty.join(', ') +
                _('. Оставьте хотя бы один узел, иначе Podkop не запустится.')), 'danger');
            return;
        }

        if (!jobs.length) {
            ui.addNotification(null, E('p', {}, _('Не выбрано ни одного сервера ни в одной секции!')), 'danger');
            return;
        }

        ui.showModal(_('Сохранение'), [ E('p', { 'class': 'spinning' },
            _('Запись выбранных узлов в секции: ') + jobs.map(function(j) { return j.sec; }).join(', ') + '...') ]);

        var task = function() {
            var nonce = self.newSaveNonce();
            var payload = JSON.stringify({
                nonce: nonce,
                jobs: jobs.map(function(job) {
                    return { section: job.sec, links: job.links,
                             selection: job.selection };
                })
            });
            return fs.write('/tmp/subparser_selected.txt', payload).then(function() {
                return fs.exec('/usr/bin/subparser-helper.sh', ['save_selected', jobs[0].sec, nonce]);
            }).then(function(res) {
                var data = {};
                try { data = JSON.parse((res.stdout || '').trim()); } catch(e) {}
                if (!data || data.status !== 'ok')
                    throw new Error((data && data.message) || _('Ошибка записи'));
            }).then(function() {
                return self.waitForRestart(nonce);
            });
        };

        this.enqueueSave(task).then(function() {
            ui.hideModal();
            ui.addNotification(null, E('p', {}, _('Серверы сохранены в Podkop. Секций: ') + jobs.length), 'info');
            jobs.forEach(function(job) { self.clearPingCache(job.sec); });
            return self.reloadDashboard(root);
        }).catch(function(err) {
            ui.hideModal();
            ui.addNotification(null, E('p', {}, _('Ошибка сохранения: ') + (err.message || err)), 'danger');
        });
    }
});
