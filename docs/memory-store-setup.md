# Как подключить новый стор памяти

Стор — отдельный git-репозиторий с памятью ролей одной инсталляции. Своя
инсталляция — свой стор: содержимое отдаётся клиенту, и смешивать в нём две
команды нельзя. Проверено на четвёртом сторе 14.09.2026 (DEV-85): по этой
инструкции он заводится и убирается, кода фабрики править не нужно.

Всё, что ниже, — шесть шагов. Подстановки: `<имя>` — короткое имя инсталляции
(`client-b`, `client-a`), `<хост>` — сервер, на котором она живёт.

## 1. Репозиторий

Приватный и пустой. Публичный стор отдал бы наружу внутренности клиента.

```bash
gh repo create beskrovnykh/agent-memory-<имя> --private \
  --description "Память ролей инсталляции <имя>"
```

## 2. Ключ и алиас

Ключ **отдельный на стор**, не личный ключ человека: личную учётку блокировали
дважды за один день, и вместе с ней встало бы всё, что ею ходит.

На хосте:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_memory_<имя> -N "" -C "agent-memory-<имя>@<хост>"
gh repo deploy-key add ~/.ssh/id_memory_<имя>.pub \
  --repo beskrovnykh/agent-memory-<имя> --allow-write --title "<хост>"
```

Алиас в `~/.ssh/config` — чтобы адрес репозитория не зависел от того, каким
ключом ходит остальной git на этом хосте:

```
Host github-memory-<имя>
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_memory_<имя>
  IdentitiesOnly yes
```

Проверка — до всего остального, иначе поломка вылезет через два часа тишиной
крона:

```bash
git ls-remote github-memory-<имя>:beskrovnykh/agent-memory-<имя>.git
```

## 3. Клон и раскладка

```bash
git clone github-memory-<имя>:beskrovnykh/agent-memory-<имя>.git \
  /home/ubuntu/agent-memory-<имя>
cd /home/ubuntu/agent-memory-<имя>
mkdir -p agent-work agent-verify agent-intake agent-incident methodology
printf '%s\n' '*.log' '*.tmp' > .gitignore
git add -A && git commit -m "стор инсталляции <имя>" && git push -u origin HEAD
```

Каталоги ролей — пустые, их наполняют сессии. `methodology/` — каталог
переопределений: файл с тем же именем, что базовая страница, читается вместо
неё целиком; файл с `drop: true` во frontmatter убирает базовую страницу.

## 4. Переменная окружения

В файле окружения раннера (`~/.agent-runner-<имя>-env`):

```
AGENT_MEMORY_STORE=/home/ubuntu/agent-memory-<имя>
```

Если у инсталляции своя дисциплина — там же:

```
AGENT_METHODOLOGY_TRACKS=разработка
```

Пусто — методология показывается вся. Названа дисциплина — показываются её
страницы плюс всё с `track: общее`. Так схема принимает команду маркетинга или
контента, не переделываясь под неё.

Слой о человеке общий для инсталляций одного владельца и лежит **вне** стора —
стор отдаётся клиенту, привычки владельца туда не помещаются:

```
AGENT_PERSON_MEMORY=/home/ubuntu/person-memory-andrey/memory/<scope>
```

**Раннер читает окружение один раз при старте.** Правка без рестарта до сессий
не доедет, и признака этого нет:

```bash
sudo systemctl restart agent-runner
```

## 5. Крон

Синхронизация — один скрипт на все сторы хоста, список в файле. Новый стор
подключается строкой, а не правкой скрипта.

```bash
install -m 755 core/memstore-sync.sh /home/ubuntu/memstore-sync.sh
echo /home/ubuntu/agent-memory-<имя> >> ~/.agent-memory-stores
( crontab -l; echo '*/2 * * * * /home/ubuntu/memstore-sync.sh >> /home/ubuntu/agent-memsync.log 2>&1' ) | crontab -
```

Префиксы строки списка:

| префикс | значение |
|---|---|
| нет | обычный стор: pull, коммит, push |
| `?` | стора может не быть на этом хосте (внешний диск) — это не поломка |
| `<` | только чтение: pull есть, коммита и push нет. Так подключают слой о человеке |

Сочетаются: `<?` — читается и может отсутствовать.

## 6. Проверка круга — наблюдением, а не обещанием

Синхронизация ломалась молча и чинилась руками; поэтому стор не считается
подключённым, пока круг не показан обеими сторонами.

```bash
# на хосте
cd /home/ubuntu/agent-memory-<имя> && mkdir -p _sync-check
date -u +%FT%TZ > _sync-check/from-<хост>.txt
bash ~/memstore-sync.sh

# на ноутбуке
git clone https://github.com/beskrovnykh/agent-memory-<имя>.git ~/agent-memory/<имя>
cat ~/agent-memory/<имя>/_sync-check/from-<хост>.txt     # правка хоста видна

echo ноутбук > ~/agent-memory/<имя>/_sync-check/from-laptop.txt
bash ~/agent-memory/memstore-sync.sh ~/agent-memory/<имя>

# снова на хосте
bash ~/memstore-sync.sh && cat /home/ubuntu/agent-memory-<имя>/_sync-check/from-laptop.txt
```

Клон на ноутбуке кладут **на внутренний диск**. `~/Workspace` — симлинк на
внешний диск; стор, положенный туда, исчезает вместе с флешкой, а
launchd отказывается заводить задание с журналом на внешнем томе.

Отметка живости лежит в `<стор>/.git/last-sync-ok`. Её отсутствие отличает
«синхронизировать было нечего» от «синхронизация не запускалась вовсе» — это
ровно тот случай, который раньше проходил незамеченным.

## Как убрать стор

Ничего не удаляя: строку из `~/.agent-memory-stores` убрать, `AGENT_MEMORY_STORE`
вернуть к прежнему значению, раннер перезапустить, каталог клона увести в
подкаталог (`mv .../agent-memory-<имя> .../снятые/`). Репозиторий на GitHub
оставить — он и есть история, ради которой стор заводили.

**Ключ при этом отзывают, а каталог — нет.** Снятый стор оставляет на хосте
живой deploy-key с правом записи в репозиторий, которым никто больше не
пользуется и за которым никто не следит:

```bash
gh repo deploy-key list --repo beskrovnykh/agent-memory-<имя>
gh repo deploy-key delete <id> --repo beskrovnykh/agent-memory-<имя>
mv ~/.ssh/id_memory_<имя> ~/.ssh/снятые/    # приватная половина тоже уходит
```

Строку `Host github-memory-<имя>` из `~/.ssh/config` убрать: алиас, ведущий на
несуществующий ключ, даёт ошибку, которую потом ищут не там.
