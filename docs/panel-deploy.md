# Веб-панель отбора сценариев — доступ из интернета

## Как устроено

```
телефон / браузер
   │  https://v3242400.hosted-by-vdsina.ru   (логин admin + PANEL_PASSWORD)
   ▼
сервер VDSina (1 ГБ, Ubuntu 22.04)
   Caddy в Docker: HTTPS (Let's Encrypt) → 127.0.0.1:8765 на сервере
   │  обратный SSH-туннель (пользователь shortstunnel, ключ .ssh/shorts_tunnel)
   ▼
ПК с конвейером
   python -m panel → 127.0.0.1:8765  (сцены, очередь, готовые видео — локально)
```

- Панель работает **на ПК** рядом с конвейером: читает `output/scenes`, очередь и видео напрямую,
  на сервер ничего не синхронизируется. Сервер только принимает HTTPS и пробрасывает запросы в туннель.
- Когда ПК выключен или туннель не запущен, сайт отвечает «Панель недоступна…».
- Пароль проверяет сама панель (HTTP Basic поверх HTTPS): `PANEL_PASSWORD` в `.env` на ПК.
- Туннельный пользователь `shortstunnel` на сервере без оболочки; его ключ разрешает **только**
  открыть порт `127.0.0.1:8765` на сервере (`restrict,port-forwarding,permitlisten=…`).
- На сервере заняты только порты 80/443 (Caddy). VPN Outline и остальные контейнеры не затрагиваются,
  фаервол не меняется.

## Запуск на ПК

```bash
powershell -ExecutionPolicy Bypass -File scripts\panel_online.ps1
```

Скрипт запускает панель (если ещё не запущена) и держит туннель с автопереподключением.
Обработчик очереди панель запускает сама, когда вы одобряете сценарий или жмёте «Сгенерировать».

Только локально, без интернета: `venv\Scripts\python -m panel` → http://127.0.0.1:8765

## Первичная настройка сервера

1. Ключ туннеля на ПК (приватная часть остаётся в `.ssh/`, в git не попадает):
   ```bash
   ssh-keygen -t ed25519 -N "" -C shorts-tunnel -f .ssh/shorts_tunnel
   ```
2. Сервер (идемпотентно):
   ```bash
   ssh root@195.133.56.48 'bash -s' -- v3242400.hosted-by-vdsina.ru "$(cat .ssh/shorts_tunnel.pub)" < scripts/server_setup.sh
   ```
3. `PANEL_PASSWORD=...` в `.env`, перезапустить панель.

## Сменить пароль

Поменять `PANEL_PASSWORD` в `.env` и перезапустить панель (закрыть окно `panel_online.ps1`
и запустить заново).

## Убрать с сервера

```bash
ssh root@195.133.56.48 "docker rm -f shorts-panel-caddy; docker volume rm shorts_caddy_data shorts_caddy_config; rm -rf /opt/shorts-panel; userdel -r shortstunnel"
```
