# 🪶 Slim-сборка sing-box для роутеров с малым объёмом флеш-памяти

Обычный `sing-box-extended` занимает **~95 МБ** и на роутерах с флеш-памятью 128 МБ и меньше
забивает `/overlay` под 100 %. После этого перестают сохраняться настройки (`uci commit`),
ломаются логи и обновления пакетов.

Slim-сборка — тот же `sing-box-extended` (`v1.14.3-extended-2.7.3`), но собранный только с
теми возможностями, которые реально используются вместе с **Podkop** и **SubParser**:

| | Обычная сборка | Slim |
|---|---|---|
| Размер бинарника | ~95 МБ | **~44 МБ** |
| Размер архива | ~40 МБ | **~15 МБ** |
| Build tags | 23 | **5** |
| `/overlay` на AX3000T | 100 % (0 свободно) | ~63 % (~20 МБ свободно) |

## ✅ Что остаётся работать

* XHTTP
* VLESS encryption (*Xray vlessenc*, `mlkem768x25519plus`)
* Reality через uTLS (`with_utls`)
* Fallback
* Failover
* DNS fallback
* Hysteria2 (и остальные QUIC-протоколы: TUIC, Hysteria, DoH3 — их включает `with_quic`)
* Clash API для Podkop (`with_clash_api`)
* TPROXY-инбаунд, `direct`, `mixed`, `selector`, `urltest`

XHTTP, VLESS encryption, Fallback, Failover и DNS fallback вообще не гейтятся build-тегами —
они есть в любой сборке, включая эту.

## ❌ Что вырезано (и почему это нормально)

| Тег | Что даёт | Нужно ли |
|---|---|---|
| `with_gvisor` | TUN-стек gVisor | Podkop работает через TPROXY, TUN не использует |
| `with_admin_panel` | встроенная веб-панель sing-box | у Podkop свой дашборд через Clash API |
| `with_manager` | Node Manager API | часть admin panel |
| `with_profiler`, `with_usbip`, `with_cloudflared` | профилировщик, USB/IP, cloudflared | не используются |
| `with_wireguard`, `with_tailscale`, `with_openvpn`, `with_openconnect` | соответствующие outbound/endpoint | только если у вас такие узлы |
| `with_dhcp`, `with_acme`, `with_ccm`, `with_ocm` и пр. | серверные/редкие функции | не используются |

> ⚠️ Если в вашем конфиге есть WireGuard-endpoint, Tailscale, OpenVPN, Cloudflared или
> TUN-инбаунд — slim-сборка не подойдёт, `sing-box check` это покажет. Нужна пересборка
> с соответствующим тегом (см. раздел «Сборка из исходников»).

## 📦 Поддерживаемые архитектуры

Архивы приложены к релизу
**[slim-v1.14.3-extended-2.7.3](https://github.com/ib0lit/SubParser/releases/tag/slim-v1.14.3-extended-2.7.3)**.
Контрольные суммы — в [`sha256sums.txt`](sha256sums.txt).

| Файл | `uname -m` | Типичные устройства |
|---|---|---|
| `sing-box-slim-amd64.tar.gz` | `x86_64` | x86-роутеры, мини-ПК |
| `sing-box-slim-386.tar.gz` | `i686` | старые x86 |
| `sing-box-slim-arm64.tar.gz` | `aarch64` | MediaTek Filogic (AX3000T, AX4200, GL-MT3000), Raspberry Pi 4/5 |
| `sing-box-slim-armv7.tar.gz` | `armv7l` | Cortex-A7/A9/A53 (32-bit) |
| `sing-box-slim-armv6.tar.gz` | `armv6l` | BCM27xx, старые ARM-роутеры |
| `sing-box-slim-armv5.tar.gz` | `armv5tel` | совсем старые ARM (Kirkwood и пр.) |
| `sing-box-slim-mips.tar.gz` | `mips` | Atheros/MediaTek MIPS (24kc) |
| `sing-box-slim-mipsle.tar.gz` | `mipsel` | большинство MIPS-роутеров (mt7621) |
| `sing-box-slim-mips64.tar.gz` | `mips64` | MIPS64 big-endian |
| `sing-box-slim-mips64le.tar.gz` | `mips64el` | MIPS64 little-endian |
| `sing-box-slim-riscv64.tar.gz` | `riscv64` | RISC-V платы |

Архитектуру роутера можно узнать командой `uname -m`.

---

## 🚀 Установка одной командой

На роутере, по SSH:

```sh
sh -c "$(curl -fsSL https://raw.githubusercontent.com/ib0lit/SubParser/slim-v1.14.3-extended-2.7.3/slim/install-slim.sh)"
```

Скрипт сам определит архитектуру, скачает нужный архив, сверит SHA256, проверит конфиг
**до** замены, остановит Podkop, заменит `/usr/bin/sing-box`, запустит сервис и покажет статус.

Полезные режимы:

```sh
# только проверить (ничего не менять)
sh -c "$(curl -fsSL .../install-slim.sh)" -- --check

# без интерактивного подтверждения
sh -c "$(curl -fsSL .../install-slim.sh)" -- --yes

# откат на сохранённый ранее бинарник
sh install-slim.sh --rollback /tmp/sing-box-orig
```

## 🖥 Установка через WinSCP + SSH

Если `curl` на роутере нет или удобнее файловым менеджером:

1. **WinSCP** → подключиться к `192.168.1.1`, логин `root`.
   * Протокол `SFTP`; если не подключается — `SCP` (на OpenWrt часто нет `openssh-sftp-server`).
2. Перейти в папку **`/tmp`** (в адресной строке ввести `/tmp`, Enter).
   `/tmp` — это RAM, файлы не занимают флеш и очищаются при перезагрузке.
3. Перетащить туда **два файла**:
   * `sing-box-slim-<arch>.tar.gz` — скачать со страницы
     [релиза](https://github.com/ib0lit/SubParser/releases/tag/slim-v1.14.3-extended-2.7.3)
     (выбрать свою архитектуру по таблице выше);
   * `install-slim.sh` — из каталога [`slim/`](.) этого репозитория.
4. Скачать на ПК бэкапы (перетащить справа налево):
   * `/usr/bin/sing-box` → `sing-box-orig`
   * `/etc/sing-box/config.json` → `config-orig.json`
   * `/etc/config/podkop` → `podkop-orig.txt`
5. Открыть PowerShell и подключиться:

   ```powershell
   ssh root@192.168.1.1
   ```

6. На роутере:

   ```sh
   cd /tmp
   sh install-slim.sh
   ```

   Скрипт найдёт архив рядом с собой (`/tmp`) и не будет ничего скачивать.

## 🔍 Проверка после установки

```sh
sing-box version                      # 1.14.3-extended-slim
podkop check_sing_box                 # все поля = 1
nft list table inet PodkopTable | grep -c tproxy    # 2
wget -qO- http://192.168.1.1:9090/version           # JSON от Clash API
df -h /overlay                        # свободного места стало больше
```

Затем `reboot` и повторить — Podkop должен сам поднять sing-box.

> `service podkop status` может показывать `not running`: procd запускает одноразовый
> `/usr/bin/podkop start`, который завершается. Ориентируйтесь на `podkop check_sing_box`.

## ↩️ Откат

Раньше сохранить старый бинарник можно только на ПК — на роутере для копии 95 МБ места нет.

```sh
# залить sing-box-orig обратно в /tmp (scp/WinSCP), затем:
sh /tmp/install-slim.sh --rollback /tmp/sing-box-orig
```

Либо вручную:

```sh
service podkop stop
cat /tmp/sing-box-orig > /usr/bin/sing-box
chmod 755 /usr/bin/sing-box
service podkop start
```

## 🔄 После обновления пакетов

`apk upgrade` (или `opkg upgrade`) вернёт стоковый бинарник из пакета `sing-box-extended`.
После каждого обновления проверяйте:

```sh
sing-box version
```

Если версия снова `*-extended-2.7.x` без суффикса `slim` — просто повторите установку.
Сам пакет **удалять нельзя**: в нём лежат `/etc/init.d/sing-box` и `/etc/config/sing-box`,
без которых Podkop не запустится.

## 🛠 Сборка из исходников

```sh
git clone --depth 1 --branch extended https://github.com/shtorm-7/sing-box-extended.git
cd sing-box-extended

CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath \
  -tags "with_quic,with_utls,with_clash_api,badlinkname,tfogo_checklinkname0" \
  -ldflags "-X 'github.com/sagernet/sing-box/constant.Version=1.14.3-extended-slim' \
            -X runtime.godebugDefault=multipathtcp=0,tlssha1=1 -checklinkname=0 -s -w -buildid=" \
  ./cmd/sing-box
```

Дополнительные переменные для других архитектур:

| `GOARCH` | доп. переменная |
|---|---|
| `arm` | `GOARM=7` / `6` / `5` |
| `mips`, `mipsle` | `GOMIPS=softfloat` |
| `mips64`, `mips64le` | `GOMIPS64=softfloat` |

Требуется Go 1.26.4. Собирать нужно именно `go build`, а не `make`: в `make` по умолчанию
включён `with_admin_panel`, для которого нужен собранный фронтенд.

## 📄 Лицензия

sing-box распространяется под **GPL-3.0-or-later**. Исходный код:
<https://github.com/shtorm-7/sing-box-extended> (ветка `extended`, тег `v1.14.3-extended-2.7.3`,
коммит `d9442d1`). Команда сборки приведена выше — она полностью воспроизводит эти бинарники.
