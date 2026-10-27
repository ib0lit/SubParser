'use strict';
'require view';
'require form';
'require fs';
'require ui';

return view.extend({
    serverLinks: [],
    availableSections: ['main'],
    githubBranches: ['main'],

    load: function() {
        var self = this;
        return fs.exec('/usr/bin/subparser-helper.sh', ['get_podkop_sections']).then(function(res) {
            try { self.availableSections = JSON.parse(res.stdout.trim() || '["main"]'); } catch(e) { self.availableSections = ['main']; }
            var initialSec = (self.availableSections.indexOf('main') !== -1) ? 'main' : (self.availableSections[0] || 'main');
            return fs.exec('/usr/bin/subparser-helper.sh', ['get_links', initialSec]);
        }).then(function(res) {
            try { return JSON.parse(res.stdout.trim() || '[]'); } catch(e) { return []; }
        }).catch(function() {
            self.availableSections = ['main'];
            return [];
        }).then(function(links) {
            return fs.exec('/usr/bin/subparser-helper.sh', ['get_github_branches']).then(function(res) {
                try { self.githubBranches = JSON.parse(res.stdout.trim() || '["main"]'); }
                catch(e) { self.githubBranches = ['main']; }
                return links;
            }).catch(function() {
                self.githubBranches = ['main'];
                return links;
            });
        });
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

    renderCards: function(container, sec) {
        var self = this;
        var s = sec || 'main';
        container.innerHTML = '';
        if (!this.serverLinks || this.serverLinks.length === 0) {
            container.appendChild(E('div', { 'style': 'padding: 15px; color: #888;' }, _('В выбранной секции Podkop [' + s + '] нет серверов.')));
            return;
        }
        var grid = E('div', { 'style': 'display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-top: 15px;' });
        this.serverLinks.forEach(function(link, idx) {
            var info = self.parseNodeInfo(link);
            var tag = s + '-' + (idx + 1) + '-out';
            var card = E('div', {
                'id': 'card_' + tag,
                'style': 'background: #1e1e1e; border: 1px solid #333; border-radius: 8px; padding: 12px 14px; display: flex; flex-direction: column; justify-content: space-between; color: #eee; box-shadow: 0 2px 4px rgba(0,0,0,0.2);'
            }, [
                E('div', { 'style': 'display: flex; align-items: flex-start; justify-content: space-between; gap: 8px;' }, [
                    E('div', { 'style': 'font-weight: 600; font-size: 13px; line-height: 1.3; word-break: break-word; flex: 1;' }, info.name),
                    E('input', { 'type': 'checkbox', 'checked': true, 'data-link': link, 'style': 'cursor: pointer; width: 18px; height: 18px; margin-top: 2px;' })
                ]),
                E('div', { 'style': 'display: flex; align-items: center; justify-content: space-between; margin-top: 14px; font-size: 12px;' }, [
                    E('span', { 'style': 'color: #999; text-transform: uppercase; font-weight: 500;' }, info.proto),
                    E('span', { 'id': 'badge_' + tag, 'class': 'node-delay-badge', 'data-tag': tag, 'style': 'color: #00e676; font-weight: bold; font-family: monospace; font-size: 13px;' }, '--- ms')
                ])
            ]);
            grid.appendChild(card);
        });
        container.appendChild(grid);
    },

    render: function(loadedLinks) {
        var self = this;
        this.serverLinks = loadedLinks || [];
        var m = new form.Map('subparser', _('SubParser'), _('Многопоточный парсер подписок с тонкой настройкой секций Podkop и дедупликацией.'));

        var s = m.section(form.NamedSection, 'settings', 'subparser', _('Настройки сервиса'));
        s.tab('general', _('⚙ Основные настройки'));
        s.tab('sections', _('📁 Секции Podkop'));
        s.tab('telegram', _('💬 Telegram'));
        s.tab('subscriptions', _('📋 Подписки'));
        s.tab('advanced', _('🛠 Дополнительно'));

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

        o = s.taboption('general', form.ListValue, 'max_best_nodes', _('Лимит лучших узлов'));
        o.value('0', _('Неограниченно'));
        o.value('3', _('Топ-3'));
        o.value('5', _('Топ-5'));
        o.value('10', _('Топ-10'));
        o.default = '0';

        o = s.taboption('general', form.Flag, 'filter_ru', _('Фильтровать RU / обход узлы'));
        o.default = '1';

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
                                self.reloadSections(secSelect, dashboardContainer, currentSec).then(function(newSec) {
                                    currentSec = newSec;
                                    ui.addNotification(null, E('p', _('Синхронизация завершена. Список узлов обновлён.')), 'info');
                                });
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

        var glob_mode = s.taboption('sections', form.ListValue, 'update_mode', _('Режим обновления'));
        glob_mode.value('replace', _('Заменять все серверы'));
        glob_mode.value('append', _('Добавлять к существующим (без дубликатов)'));
        glob_mode.default = 'replace';
        glob_mode.depends('per_section_config', '0');

        self.availableSections.forEach(function(sec) {
            var sec_en = s.taboption('sections', form.Flag, 'sec_en_' + sec, _('Обновлять секцию') + ' [' + sec + ']');
            sec_en.default = '1';
            sec_en.depends('per_section_config', '1');

            var sec_m = s.taboption('sections', form.ListValue, 'sec_mode_' + sec, _('Режим для') + ' [' + sec + ']');
            sec_m.value('replace', _('Заменять все серверы'));
            sec_m.value('append', _('Добавлять к существующим (без дубликатов)'));
            sec_m.default = 'replace';
            sec_m.depends('per_section_config', '1');
        });

        o = s.taboption('advanced', form.Value, 'cache_ttl', _('Время жизни кэша результатов (сек)'));
        o.datatype = 'uinteger';
        o.default = '900';
        o.description = _('Повторный запуск с теми же подписками и настройками возьмёт результаты из кэша. 0 — отключить кэш.');

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

        var s2 = m.section(form.NamedSection, 'settings', 'subparser', _('🌐 Активные узлы в конфигурации Podkop'));
        var dashboardContainer = E('div', { 'id': 'subparser_dashboard_box' });

        var currentSec = (self.availableSections && self.availableSections.indexOf('main') !== -1)
            ? 'main'
            : ((self.availableSections && self.availableSections[0]) ? self.availableSections[0] : 'main');

        var hintBox = E('details', {
            'class': 'alert-message info',
            'style': 'margin-bottom: 12px; font-size: 13px; cursor: pointer;'
        }, [
            E('summary', { 'style': 'font-weight: bold; outline: none;' }, _('💡 Как работать со списком узлов (нажмите, чтобы раскрыть)')),
            E('ul', { 'style': 'margin: 8px 0 0 18px; padding: 0; line-height: 1.5; cursor: default;' }, [
                E('li', {}, _('Выберите нужный профиль Podkop в выпадающем списке секций.')),
                E('li', {}, _('Нажмите «⚡ Обновить пинги», чтобы измерить задержку отклика серверов через ядро.')),
                E('li', {}, _('Снимите галочки с проблемных серверов или оставьте только нужные.')),
                E('li', {}, _('Нажмите «💾 Применить выбранные узлы» для перезаписи конфигурации профиля и рестарта Podkop.'))
            ])
        ]);

        var secSelect = E('select', {
            'class': 'cbi-input-select',
            'style': 'min-width: 140px; margin-right: 10px; font-weight: bold;',
            'change': function(ev) {
                currentSec = ev.target.value;
                fs.exec('/usr/bin/subparser-helper.sh', ['get_links', currentSec]).then(function(res) {
                    try { self.serverLinks = JSON.parse(res.stdout.trim() || '[]'); } catch(e) { self.serverLinks = []; }
                    self.renderCards(dashboardContainer, currentSec);
                });
            }
        });
        self.availableSections.forEach(function(sname) {
            var optAttrs = { 'value': sname };
            if (sname === currentSec) {
                optAttrs.selected = 'selected';
            }
            secSelect.appendChild(E('option', optAttrs, _('Секция: ') + sname));
        });

        var toolbar = E('div', { 'style': 'display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 10px;' }, [
            secSelect,
            E('button', {
                'class': 'btn cbi-button-neutral',
                'click': function(ev) {
                    ev.preventDefault();
                    self.reloadSections(secSelect, dashboardContainer, currentSec).then(function(newSec) {
                        currentSec = newSec;
                        ui.addNotification(null, E('p', _('Список секций Podkop обновлён.')), 'info');
                    });
                }
            }, _('🔄 Обновить секции')),
            E('button', {
                'class': 'btn cbi-button-action',
                'click': function(ev) { ev.preventDefault(); self.refreshPings(dashboardContainer, currentSec); }
            }, _('⚡ Обновить пинги')),
            E('button', {
                'class': 'btn cbi-button-positive',
                'click': function(ev) { ev.preventDefault(); self.saveSelectedNodes(dashboardContainer, currentSec); }
            }, _('💾 Применить выбранные узлы')),
            E('button', {
                'class': 'btn cbi-button-neutral',
                'click': function(ev) {
                    ev.preventDefault();
                    var cbs = dashboardContainer.querySelectorAll('input[type="checkbox"]');
                    var allChecked = Array.from(cbs).every(function(c) { return c.checked; });
                    cbs.forEach(function(c) { c.checked = !allChecked; });
                }
            }, _('Выделить все / Снять'))
        ]);
        var pingSummary = E('div', {
            'id': 'subparser_ping_summary',
            'style': 'font-size: 12px; color: #888; margin-bottom: 8px;'
        }, '');

        var dummy = s2.option(form.DummyValue, '_dashboard');
        dummy.rawhtml = true;
        dummy.render = function() { return E('div', {}, [hintBox, toolbar, pingSummary, dashboardContainer]); };
        self.renderCards(dashboardContainer, currentSec);

        return m.render();
    },

    reloadLinks: function(container, sec) {
        var self = this;
        var s = sec || 'main';
        return fs.exec('/usr/bin/subparser-helper.sh', ['get_links', s]).then(function(res) {
            try { self.serverLinks = JSON.parse(res.stdout.trim() || '[]'); } catch(e) { self.serverLinks = []; }
            self.renderCards(container, s);
            var summary = document.getElementById('subparser_ping_summary');
            if (summary) summary.textContent = _('Секция ') + s + ': ' + self.serverLinks.length + _(' узлов.');
            return self.serverLinks;
        });
    },

    reloadSections: function(selectEl, container, currentSec) {
        var self = this;
        return fs.exec('/usr/bin/subparser-helper.sh', ['get_podkop_sections']).then(function(res) {
            var sections = ['main'];
            try {
                var parsed = JSON.parse(res.stdout.trim() || '["main"]');
                if (Array.isArray(parsed) && parsed.length > 0) sections = parsed;
            } catch(e) {}
            self.availableSections = sections;
            var keep = (sections.indexOf(currentSec) !== -1) ? currentSec : sections[0];
            while (selectEl.firstChild) selectEl.removeChild(selectEl.firstChild);
            sections.forEach(function(sname) {
                var optAttrs = { 'value': sname };
                if (sname === keep) optAttrs.selected = 'selected';
                selectEl.appendChild(E('option', optAttrs, _('Секция: ') + sname));
            });
            return self.reloadLinks(container, keep).then(function() { return keep; });
        });
    },

    refreshPings: function(container, sec) {
        var s = sec || 'main';
        var badges = Array.from(container.querySelectorAll('.node-delay-badge'));
        badges.forEach(function(b) { b.innerText = '...'; b.style.color = '#888'; });
        var summary = document.getElementById('subparser_ping_summary');
        if (summary) summary.textContent = _('Измерение задержек...');

        var setBadge = function(b, delay) {
            if (typeof delay === 'number' && delay > 0) {
                b.innerText = delay + ' ms';
                b.style.color = delay < 180 ? '#00e676' : (delay < 350 ? '#ffb300' : '#ff5252');
                return true;
            }
            b.innerText = 'timeout';
            b.style.color = '#ff5252';
            return false;
        };

        fs.exec('/usr/bin/subparser-helper.sh', ['ping_group', s]).then(function(res) {
            var groupDelays = {};
            try { groupDelays = JSON.parse(res.stdout.trim() || '{}'); } catch(e) {}
            var okCount = 0;
            var missing = [];

            badges.forEach(function(b) {
                var tag = b.getAttribute('data-tag');
                if (Object.prototype.hasOwnProperty.call(groupDelays, tag) && setBadge(b, groupDelays[tag])) {
                    okCount++;
                } else {
                    b.innerText = '...';
                    missing.push(b);
                }
            });

            // Ограничиваем параллелизм: роутер не любит десятки одновременных
            // вызовов uci/curl, а зависшие узлы ждут таймаут.
            var CONCURRENCY = 4;
            var index = 0;
            var failed = 0;

            var next = function() {
                if (index >= missing.length) {
                    if (summary) {
                        summary.textContent = _('Секция ') + s + ': ' + okCount + _(' из ') + badges.length +
                            _(' узлов отвечают') + (failed ? _(' (') + failed + _(' с таймаутом)') : '');
                    }
                    return;
                }
                var b = missing[index++];
                var tag = b.getAttribute('data-tag');
                fs.exec('/usr/bin/subparser-helper.sh', ['ping_node', tag]).then(function(nodeRes) {
                    var data = {};
                    try { data = JSON.parse(nodeRes.stdout.trim()); } catch(err) {}
                    if (setBadge(b, data.delay)) okCount++; else failed++;
                }).catch(function() {
                    setBadge(b, -1);
                    failed++;
                }).then(next);
            };

            if (missing.length === 0) {
                if (summary) {
                    summary.textContent = _('Секция ') + s + ': ' + okCount + _(' из ') + badges.length + _(' узлов отвечают');
                }
            } else {
                for (var i = 0; i < Math.min(CONCURRENCY, missing.length); i++) next();
            }
        });
    },

    saveSelectedNodes: function(container, sec) {
        var self = this;
        var s = sec || 'main';
        var checkboxes = container.querySelectorAll('input[type="checkbox"]:checked');
        var selectedLinks = [];
        checkboxes.forEach(function(cb) {
            var link = cb.getAttribute('data-link');
            if (link) selectedLinks.push(link);
        });

        if (selectedLinks.length === 0) {
            ui.addNotification(null, E('p', {}, _('Выберите хотя бы один сервер!')), 'danger');
            return;
        }

        ui.showModal(_('Сохранение'), [ E('p', { 'class': 'spinning' }, _('Запись выбранных узлов в секцию ' + s + ' (' + selectedLinks.length + ' шт.)...')) ]);
        var payload = selectedLinks.join('\n') + '\n';

        fs.write('/tmp/subparser_selected.txt', payload).then(function() {
            return fs.exec('/usr/bin/subparser-helper.sh', ['save_selected', s]);
        }).then(function(res) {
            var data = {};
            try { data = JSON.parse((res.stdout || '').trim()); } catch(e) {}
            if (data.status && data.status !== 'ok') throw new Error(data.message || _('Ошибка записи'));
            ui.hideModal();
            ui.addNotification(null, E('p', {}, _('Серверы успешно сохранены в Podkop! (' + selectedLinks.length + ' шт.)')), 'info');
            return self.reloadLinks(container, s);
        }).catch(function(err) {
            ui.hideModal();
            ui.addNotification(null, E('p', {}, _('Ошибка сохранения: ') + (err.message || err)), 'danger');
        });
    }
});
