#!/usr/bin/env bash
# Синхронизация сторов памяти через git. Один скрипт на все сторы хоста.
#
# Почему список сторов лежит в файле, а не в самом скрипте: прежняя версия
# зашивала путь константой (`REPO='/home/ubuntu/agent-memory-client-b'`), и
# четвёртый стор требовал править скрипт на каждом хосте. Теперь стор
# подключается строкой в `~/.agent-memory-stores` — правки кода фабрики не
# нужно (DEV-85).
#
# Почему ошибки НЕ глушатся: прежняя версия писала `2>/dev/null || true` на
# каждом шаге, и синхронизация, сломавшись, продолжала выглядеть работающей.
# Сломалась она ровно так и чинилась руками. Здесь каждый стор докладывает
# исход строкой, а ошибка git печатается целиком — журнал крона и есть прибор.
#
# Использование:
#   memstore-sync.sh                 # сторы из ~/.agent-memory-stores
#   memstore-sync.sh /путь/к/стору   # один стор, минуя список
#
# Префиксы строки списка:
#   `?`  стор может отсутствовать на этом хосте, и это штатно: личный стор лежит
#        на внешнем диске, и отключённая флешка не поломка синхронизации;
#   `<`  стор только читается — pull есть, коммита и push нет. Так подключается
#        слой о человеке: его владелец — сам человек, инсталляция его читает.
#        Без этого режима первая же сессия, тронувшая файл, отправила бы правку
#        в личный репозиторий от имени агента.
# Префиксы можно сочетать: `<?` — читается и может отсутствовать.
set -uo pipefail

LIST="${AGENT_MEMORY_STORES_FILE:-$HOME/.agent-memory-stores}"
STAMP=$(date -u +%FT%TZ)
HOST=$(hostname -s)
rc_all=0

sync_one() {
  local repo="$1" optional=0 readonly_=0 name lock
  while :; do
    case "$repo" in
      "?"*) optional=1;  repo="${repo#\?}" ;;
      "<"*) readonly_=1; repo="${repo#<}" ;;
      *) break ;;
    esac
  done
  name=$(basename "$repo")
  # Замок в /tmp, а не в $TMPDIR: у launchd на macOS свой TMPDIR на сессию, и
  # замок оказался бы у каждого запуска свой — то есть взаимного исключения не
  # было бы вовсе, ровно между теми двумя, кого надо развести: заданием по
  # расписанию и запуском руками.
  lock="/tmp/memsync-${name}.lock.d"

  # Доступность проверяется не наличием каталога, а тем, что git может с ним
  # работать. Разница не теоретическая: на macOS задание launchd не получает
  # доступа к внешнему тому, каталог при этом виден, а любая операция git
  # падает на «Operation not permitted». Для помеченного необязательным стора
  # это тот же случай, что отключённая флешка, — сказать и идти дальше.
  local why=""
  if ! why=$(git -C "$repo" rev-parse --git-dir 2>&1); then
    if [ "$optional" = 1 ]; then
      echo "$STAMP $name недоступен ($repo) — помечен необязательным, это штатно"
      return 0
    fi
    echo "$STAMP $name ПРОПУЩЕН: $repo — $why"
    return 1
  fi
  # Атомарный замок без flock: на macOS его нет, а гонка двух прогонов по
  # одному стору даёт конфликт ребейза посреди коммита.
  if ! mkdir "$lock" 2>/dev/null; then
    # Прогон, убитый вместе с ssh-сессией, замок за собой не убирает, и
    # синхронизация встаёт навсегда — тихо, потому что «пропущен» выглядит
    # штатно. Замок старше STALE минут считается брошенным и забирается, но
    # об этом говорится вслух: это не норма.
    local age
    age=$(( $(date +%s) - $(stat -c %Y "$lock" 2>/dev/null || stat -f %m "$lock" 2>/dev/null || date +%s) ))
    if [ "$age" -gt "${LOCK_STALE_SEC:-600}" ]; then
      echo "$STAMP $name замок брошен ${age}с назад — забираю"
      rmdir "$lock" 2>/dev/null
      mkdir "$lock" 2>/dev/null || { echo "$STAMP $name замок не отдаётся"; return 1; }
    else
      echo "$STAMP $name пропущен: предыдущий прогон ещё идёт (${age}с)"
      return 0
    fi
  fi
  trap 'rmdir "$lock" 2>/dev/null' RETURN

  local out rc=0
  if ! out=$(git -C "$repo" pull --rebase --autostash 2>&1); then
    echo "$STAMP $name ОШИБКА pull: $out"
    rc=1
  fi
  if [ "$readonly_" = 1 ]; then
    # Стор только на чтение: локальную правку не коммитим и не отправляем, но и
    # не теряем молча — говорим о ней, чтобы человек решил сам.
    if ! git -C "$repo" diff --quiet || [ -n "$(git -C "$repo" status --porcelain)" ]; then
      echo "$STAMP $name ТОЛЬКО ЧТЕНИЕ, но в рабочей копии есть правки — не отправлены:"
      git -C "$repo" status --porcelain | sed "s/^/    /"
      rc=1
    fi
    [ $rc -eq 0 ] && date -u +%FT%TZ > "$repo/.git/last-sync-ok"
    return $rc
  fi
  if ! out=$(git -C "$repo" add -A 2>&1); then
    echo "$STAMP $name ОШИБКА add: $out"
    rc=1
  fi
  if ! git -C "$repo" diff --cached --quiet; then
    local n
    n=$(git -C "$repo" diff --cached --name-only | wc -l | tr -d ' ')
    if out=$(git -C "$repo" -c user.name="agent-$name" -c user.email="agent@$name" \
             commit -q -m "sync $HOST $STAMP" 2>&1); then
      echo "$STAMP $name коммит: $n файлов"
    else
      echo "$STAMP $name ОШИБКА commit: $out"
      rc=1
    fi
  fi
  # Пушим всегда, а не только после своего коммита: коммит мог остаться с
  # прошлого прогона, у которого не было сети.
  if ! out=$(git -C "$repo" push origin HEAD 2>&1); then
    echo "$STAMP $name ОШИБКА push: $out"
    rc=1
  fi
  # Отметка живости: её отсутствие отличает «синк молчит, потому что нечего
  # синхронизировать» от «синк не запускался вовсе».
  [ $rc -eq 0 ] && date -u +%FT%TZ > "$repo/.git/last-sync-ok"
  return $rc
}

if [ $# -gt 0 ]; then
  for repo in "$@"; do sync_one "$repo" || rc_all=1; done
  exit $rc_all
fi

if [ ! -f "$LIST" ]; then
  echo "$STAMP НЕТ СПИСКА СТОРОВ: $LIST — синхронизировать нечего"
  exit 1
fi
while IFS= read -r line; do
  line="${line%%#*}"; line="${line#"${line%%[![:space:]]*}"}"; line="${line%"${line##*[![:space:]]}"}"
  [ -n "$line" ] || continue
  sync_one "$line" || rc_all=1
done < "$LIST"
exit $rc_all
