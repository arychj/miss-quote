---
layout: page
title: Configuration
eyebrow: Reference
lede: Everything about how the bot behaves, and which servers it behaves that way in, is config.yaml. Everything a deployment points at stays in the environment. This is the whole of both.
description: The complete miss-quote configuration reference — config.yaml, the server block, every tool and its settings, the deployment-wide settings block, and every environment variable.
---

## The file

`config.yaml` is mounted at `/config/config.yaml` from a ConfigMap. Point `CONFIG_FILE` elsewhere to override the location. The file is read once at startup, so editing it means restarting the pod. The IDs in the repo copy are placeholders.

```yaml
settings:
  quotes:
    backoff_seconds: 300
  fines:
    volume_floor: 0.25

servers:
  123456789012345678:
    alias: first-server
    users:
      234567890123456789: Speaker One
    tools:
      scoreboard:
        enabled: true
      tts:
        enabled: true
      verbal-morality:
        enabled: true
        config:
          words: [fiddlesticks, poppycock]

  876543210987654321:
    alias: second-server
    users:
      234567890123456789: Someone Else
```

The split against the environment is what a deployment **points at** versus how it **behaves**. Hosts, ports, directories, and the token stay variables: they are what a manifest already carries and what a secret has to stay in. Everything that is a number or a wording — how long a trigger stays spent, what a balance is called, how quiet a repeat offender gets — is `settings:`, because twenty of those spread across a pod spec is a worse thing to read than one file with comments in it.

`settings:` is optional in its entirety, and so is every line in it: each setting has a default, and a file that mentions none of them is a working file. What it is **not** is a free-form block. A name nothing reads — a typo, or a setting written under the wrong section — is reported at startup rather than ignored, on the same reasoning as a stray key in a tool block: the alternative is a deployment running on a default against a file that plainly asks for something else. A value that will not parse falls back to its default and is reported too, rather than stopping the pod, because this is also the file that decides which servers get joined.

### Parsing reports rather than raises

A server whose block is malformed — no `alias`, or not a mapping at all — is dropped and logged at startup; the bot joins one fewer server instead of crash-looping over a typo. The same goes for a name filed under something that is not a user ID, and a tool whose settings will not parse.

On startup the bot reconciles the file against the servers it is actually in, and says so. Four things can be wrong and none of them raise: an entry would not parse, nothing is configured, a server is configured but the bot was never invited, or the bot is in a server nobody configured. Each is logged, so none has to be discovered by noticing an empty transcript directory.

## Servers

Everything about a server lives under its ID, and the ID appears there and nowhere else.

| Key | Required | Purpose |
|---|---|---|
| `alias` | yes | Names the transcript directory, so renaming a server on Discord changes nothing about where its transcripts land |
| `users` | no | Replaces the display name Discord reports for a speaker |
| `tools` | no | Elects the server into the tools listed under it |

**`servers` is a hard gate on joining.** A server that is not listed is never joined, by autojoin or by an explicit `!join`, and an empty mapping or a missing file means the bot joins nothing at all. That direction is deliberate: joining no server is something you notice and fix, while recording a server the bot should not have been in is not something you can take back.

### users

```yaml
users:
  234567890123456789: Speaker One
  "345678901234567890": Speaker Two
```

`users` replaces the display name Discord reports for a speaker. Discord nicknames are freely editable and often not a name at all, which makes them poor labels in a transcript that a summarizer will later read. The roster is per server because the same person can be known differently in two places. IDs may be quoted or bare; both are read as integers.

The roster is also **what can be warmed** and **who is eligible for the scoreboard**. A phrase naming somebody on it is rendered before anybody speaks; somebody not on it waits for the synthesizer the first time they are named, and nobody waits again.

### tools {#server-tools}

`tools` elects the server into the tools listed under it. Each is opted into on its own, **including the ones others depend on**: `verbal-morality` hands its fines to `scoreboard` and its words to `tts`, and a server that enables the first and neither of the others is fining people silently and keeping no record of it. Each absence is reported at startup rather than left to be noticed.

**A tool block holds `enabled` and `config`, and nothing else.** Every setting a tool takes goes under `config:`; written a level up, beside `enabled`, it is read by nothing. That is the one misconfiguration with no symptom — the tool starts, the log says it is enabled, and it runs on its defaults against a file that plainly asks for something else — so anything else in a tool block is named at startup alongside the other parsing problems:

```yaml
      quotes:
        enabled: true
        penalize_self_answers: false   # ← wrong: reported at startup, read by nothing
        config:
          penalize_self_answers: false # ← right
```

Two tools that require each other are a circle, reported at startup and **left unbuilt** — see [the tool contract]({{ '/about/#one-tool-calling-another' | relative_url }}).

## Tools {#tools}

Five tools ship. A name nothing answers to is reported at startup and skipped, the registry being a closed list rather than whatever happens to be importable.

### quotes {#quotes}

Answers the channel with the film line it just walked into. It listens for a trigger phrase and, on hearing one, says the associated quote out loud where it was said — and then asks where the line came from.

```yaml
quotes:
  enabled: true
  config:
    answer_seconds: 5
    tie_seconds: 1
    remarks:
      - having watched it more recently than is respectable.
```

| Setting | Required | Purpose |
|---|---|---|
| `answer_seconds` | no, `5` | How long the channel has to name the title once the line has finished playing. `0` stops the tool asking at all |
| `tie_seconds` | no, `1` | How long after the first correct answer a second one is still paid. `0` pays only whoever was first |
| `penalize_self_answers` | no, `true` | Whether whoever set a line off is barred from naming it. `false` lets them answer like anybody else |
| `self_answer_penalty` | no, `5` | What an attempt costs them, in credits. Floored at `0` |
| `remarks` | no | Endings the announcement draws from, **added** to the ones the tool ships with. A lone one may be written unquoted rather than as a list |
| `announcement` | no | What the winner is told. `{user}`, `{credits}`, and `{remark}` |
| `tie_announcement` | no | What anyone paid on a tie is told. The same placeholders |
| `self_answer_announcement` | no | What somebody naming their own line is told. The same placeholders, where `{credits}` is what it cost |
| `additional_quotes` | no | Quotes this server hears and the others do not, in the [quote file's](#the-quote-file) own shape — or a path or URL to a file holding them. Merged over the deployment's list; see [what a server adds for itself](#what-a-server-adds-for-itself) |

#### The quote file

Most of the list is per deployment. The lines come from a YAML file at `QUOTES_FILE` — a film, and under it the phrases that set its lines off — so adding a quote is a key rather than a deployment. The image ships the list in `resources/quotes.yaml`; mount your own over that path to say something it does not.

```yaml
Firefly:
  cool: Shiny.
  behave: I aim to misbehave.

Project Hail Mary:
  question: "{user} question is dumb."

The Princess Bride:
  impossible: Inconceivable!
```

| Where | Purpose |
|---|---|
| The outer key | Where the line is from. Never spoken; it is what the round asks about, and what makes the log and the file readable |
| The inner key | The phrase that sets the line off. Matched whole and case-insensitively, however the file writes it |
| The value | What gets said. `{user}` is the only placeholder, and names whoever set it off |

**A trigger appears once in the whole file.** Nesting under the title makes it a key, so writing it twice under one title is not something the format can express — and writing it under two titles is refused for the same reason, rather than being allowed to mean something a repeat under one title could not. The first is kept and the rest are reported, so the line you have to go and delete is the later one.

**Two triggers may share an answer**, which is how the file says that two phrases deserve the same reply — `awesome` and `cool` both earn `Shiny.`. There is no alternation syntax inside a trigger: a trigger is matched as written, so a key meaning to catch two phrases has to be two keys.

**A phrase worth answering several ways lists its lines, and one of them is drawn each time it fires:**

```yaml
Firefly:
  cool:
    - Shiny.
    - Gorram it.
```

The draw happens when the trigger fires rather than at startup, so a restart is not what decides which line a channel hears for the next week. The backoff is unchanged and still keyed on the trigger, so a trigger with four answers still fires once per window, not four times.

Two things are worth knowing about the format itself, because both look entirely correct in the file:

- **A line starting with `{user}` has to be quoted.** Unquoted, a `{` opens a mapping and the file will not parse.
- **A trigger like `no` or a title like `1917` has to be quoted too.** Unquoted, YAML reads them as a boolean and an integer, and neither is text the matcher can ever compare against.

The file is read at startup and **reports rather than raises**. An entry with no trigger or no line, a line carrying a placeholder nothing fills, a trigger already answering elsewhere, and anything YAML did not read as text are each logged with their line number and dropped. What *does* stop the tool from starting is a file that is missing, unreadable, not valid YAML, not a mapping of titles, or holding no usable entry at all.

**A dropped entry is a line in a log nobody reads**, which is why the file is also checked before it can be merged. `scripts/validate_quotes.py` applies the loader's rules where a broken entry fails a pull request instead, plus the ones the loader has no opinion about:

| Checked | Why |
|---|---|
| Every key and value is text | An unquoted `no` is a boolean and an unquoted `1917` is an integer; both look right and neither can ever match |
| A trigger answers under one title only | It is a key, so a repeat is either impossible or a disagreement about which line a phrase earns |
| Every part populated, and unpadded | The loader strips surrounding whitespace, so the file and what it produces disagree quietly |
| `trigger` ≤ 30 characters, `quote` ≤ 150 | A trigger has to be said in passing and a line has to land before the channel moves on |
| A trigger that could actually fire | No placeholders, no repeated whitespace, at least one letter or digit |
| `{user}` is the only placeholder | Anything else drops the entry at startup, so the symptom is a line that is never said |
| No trigger listing the same line twice | A second way of answering is the point; the same way twice is a line pasted and half-edited |
| Titles non-decreasing, LF endings, trailing newline | So the file stays reviewable and two branches adding a line do not collide |

It needs PyYAML and nothing else, which is what keeps it a few seconds on every pull request rather than an image build.

#### What a server adds for itself {#what-a-server-adds-for-itself}

A film everybody in one channel has seen is usually one everybody in the next has too, which is why the list is one file. An in-joke is not, and a server with one writes it under `additional_quotes`, in its own `config` block and in the file's own shape:

```yaml
quotes:
  enabled: true
  config:
    answer_seconds: 5
    additional_quotes:
      Firefly:
        cool: Shiny.
        behave: I aim to misbehave.
      "1917":
        over the top: "Over the top, {user}!"
```

Everything the file can say, this can say: a trigger answering several ways lists its lines, a line naming whoever set it off writes `{user}`, and the two quoting rules above still bite — a title like `1917` and a line starting with `{user}` both need their quotes. A server that would rather keep its list in a file of its own writes the name of that file here instead; see [keeping that list somewhere else](#keeping-that-list-somewhere-else).

**These are the same rules, applied per server.** A trigger appears once in one server's block, its lines are checked the same way, and an entry that fails is logged and dropped. What it does *not* do is stop the tool: the block is optional, and a server that gets it wrong still has the whole shipped list rather than nothing.

**A trigger the shipped list already answers is answered by this server's line instead.** The deployment's file is what everybody agrees on rather than what they are held to, and a server that would rather `cool` earned something else should not have to pick a different phrase to say so. It is one server's decision and one server's alone; the next server on the same deployment hears the shipped line.

**Titles merge.** The list is keyed on the trigger and carries the title beside each line, so `Firefly` written in both places is one film with everything either of them said under it — and a round asking where either line came from asks about the same title.

**They are checked in CI like the file is.** `scripts/validate_quotes.py --config config.yaml` walks every server's block and applies the table above, so a broken addition fails a pull request rather than becoming a log line. Each block is checked on its own: a trigger a server shares with the shipped list is the override, not a collision.

#### Keeping that list somewhere else {#keeping-that-list-somewhere-else}

A list long enough to be worth its own file does not have to live in `config.yaml`. Write `additional_quotes` as one string instead of the quotes themselves — a path on disk, mounted wherever the rest of the deployment's configuration is:

```yaml
quotes:
  enabled: true
  config:
    additional_quotes: /config/quotes/beer-wars.yaml
```

or somewhere to download it from, which is read once on the way up:

```yaml
quotes:
  enabled: true
  config:
    additional_quotes: https://quotes.example.com/beer-wars.yaml
```

A string beginning `http://` or `https://` is downloaded; anything else is a path, where a leading `~` is the home directory. There is no second key saying which — the scheme is the whole rule.

| | What to expect |
|---|---|
| What the file holds | Exactly what the block would have: a mapping of titles, each holding its triggers. The [file's rules](#the-quote-file) in full |
| When it is read | Once, at startup. A list that changes afterwards reaches the channel at the next restart, which is the promise `QUOTES_FILE` makes too |
| How it is merged | As an inline block is — over the deployment's list, for that server alone |
| What a download waits | 10 seconds, then the server starts without it |

**A file it cannot get is a log line and nothing worse.** Missing, unreadable, not valid YAML, not a mapping of titles, a server that will not answer — each is reported and the server keeps the whole shipped list, on the same terms as a block written inline that turns out to be nonsense. The deployment's own file is the one that stops the tool starting; a server's is not.

**A dropped entry names the line it was written on**, which is the one thing an inline block cannot do: `config.yaml` has been parsed by the time the tool sees it, and a file it points at is still a file.

**CI does not follow the name.** `validate_quotes.py --config` leaves a path or URL alone — a path in a config file is a path inside the deployment it configures, and the validator makes no network calls. Check the file behind it by passing it in like any other quote file:

```bash
python scripts/validate_quotes.py quotes/beer-wars.yaml --config config.yaml
```

#### Matching and backoff

Matching is **whole words, case-insensitive**, so `real` does not fire inside `really`. Several triggers are phrases rather than words, and a phrase matches on a single space between its words, which is what an ASR transcript holds.

**One line per utterance**, however many triggers were in the sentence: two quotes over the top of each other is a denial of service on the channel. The one that answers is the **earliest in the sentence** rather than the first in the file, since that is the one whoever spoke arrived at. Where two triggers start at the same word the longer wins.

**A trigger that has just fired goes quiet for [`settings.quotes.backoff_seconds`](#settings-quotes)**, five minutes by default. The joke is the recognition, and a channel that says "cool" four times in a minute does not want "Shiny." four times back. The window is keyed on the **trigger**, not the speaker and not the line, and is per server and held in memory only — so two channels arriving at the same line have each made the joke once, and a restart forgives every backoff.

**The whole list is rendered at startup.** The triggers are a closed set and so are the answers, so on the way up the tool hands `tts` every line in the file. A callback that arrives four seconds after the line it answers is not a callback. The exception is a line naming whoever set it off, which is rendered once per name on the roster.

#### Naming it

**A line that has been said is also a question.** For `answer_seconds` afterwards the channel can say where it came from — `what is Firefly` — and whoever does is paid a credit through [`scoreboard`](#scoreboard), the same board `verbal-morality` takes them off, and told so out loud.

The window opens when the line has **finished playing**, not when the trigger was heard. Transcription and synthesis take as long as they take, and a window that started at the trigger could be over before the channel had heard the question.

**The first correct answer takes the round, and anyone inside `tie_seconds` of it is paid as well.** Two people arriving at the same title half a second apart both knew it, and which of them the transcriber happened to return first is not a fact about who was faster. Nobody is paid twice for the same title however many times they say it.

Answers are matched forgivingly, because an ASR transcript is not punctuated the way a poster is:

| The file says | So the channel may say |
|---|---|
| `Firefly` | `what is Firefly`, `what's Firefly`, `What is Firefly?` |
| `The Matrix` | `what is the matrix`, `what is matrix` — a leading `the`, `a`, or `an` is optional either way |
| `Hitchhiker's Guide to the Galaxy` | `what is hitchhikers guide to the galaxy` — apostrophes are dropped from both sides |
| `Tucker and Dale vs Evil` | `...vs Evil`, `...vs. Evil`, `...versus Evil` |

The answer may sit anywhere in the sentence. A row with an empty `movie` asks nothing, there being no question in it. A title carrying a **numeral** is matched as a numeral — `Apollo 13` answers to `what is apollo 13` and not to `what is apollo thirteen`, which is what an ASR is likelier to return; write the title the way it will be transcribed if that matters.

**Two rounds can be open at once**, since an answer names its own title and cannot be mistaken for an answer to the other. An utterance that answers an open round is an answer and nothing else, whatever trigger it also contains — otherwise a channel naming a title could set off the line that asks about the next one, which is a loop the tool would be driving rather than following.

A server with no `scoreboard` asks the question and pays nothing, which is said once at startup rather than left to be noticed.

#### The announcement

The award is said out loud, with **no chime in front of it**. A fine opens with one because it interrupts a conversation that was about something else; an award answers a question the channel is already sitting in.

```
Correct! Erik, you are awarded 1 credit for quoting along at home.
```

**The ending is drawn fresh each time**, from the list the tool ships with plus whatever `remarks` adds to it. One fixed sentence is a joke told once and then endured. The shipped endings are:

- `knowing exactly where that came from, which explains a great deal.`
- `quoting along at home.`
- `a display of recall that has never once been useful.`
- `having excellent taste and nothing better to do.`
- `being the sort of person who knows that.`
- `spending your formative years exactly as you did.`

`remarks` **adds** to those rather than replacing them. None of the shipped endings says "film" — the key is called `movie` because it started that way, but a trigger answers for a series, a game, or a book as often as a picture, and an announcement that guesses wrong guesses wrong out loud. Write your own the same way.

Somebody paid on a **tie** gets the second wording — `Eli, you are also awarded 1 credit, for getting there at the same time.` — because the whole sentence again reads as though the bot had lost track of what it just said.

**Nothing this tool says is dropped for landing while something else is playing**, which is the one place it parts company with `verbal-morality`. Everything `quotes` says is an answer to something it just said itself. Announcements wait their turn on the speaker's per-server lock and come out in the order they were earned.

#### Naming your own line

**Whoever set a line off cannot name it.** They have the trigger and the title in front of them and had to recall neither, so a round they could win is one anybody can farm by reading the quote file out loud. An attempt is refused out loud and **costs them `self_answer_penalty` credits**:

```
Nuh uh uh. Erik, you set it off, so you do not get to name it. You are fined 5 credits for being a dick.
```

Refused rather than quietly ignored, because a rule nobody is told about is one everybody keeps testing. The penalty is deliberately larger than the single credit the attempt was worth, and it is taken **once per round** however many times they say it. An attempt **neither wins the round nor spoils it**: whoever names it next is the first answer and is paid in full. The bar is per round, not per person.

`penalize_self_answers: false` drops the rule entirely.

### summary {#summary}

Writes down what happened in a voice channel once the bot leaves it, and reads it back out loud when somebody asks. It is the only tool that uses the finished-transcript moment.

```yaml
summary:
  enabled: true
  config:
    monitored_channels:
      general-voice:
        channel: session-summaries
        schedule:
          - Wed 17:00-00:00
```

That is a working block. Everything else has a default.

#### Which channels

**Everything is per voice channel, under `monitored_channels`, and that mapping is also the switch.** A channel that is not in it is **not transcribed**, not summarized, not posted, and does not answer the question either — one rule rather than four, so a room left off the list is left off entirely. See [the capture schedule]({{ '/about/#the-capture-schedule' | relative_url }}) for what that means for the transcript, and note the consequence: **turning this tool off stops the server writing anything down.**

Per channel rather than per server because a server's rooms are not interchangeable. One is where a game night happens and one is where two people are debugging something, and a bot that summarizes every room it was ever dragged into is writing files nobody asked for and posting them where everybody can read them.

Keys are matched through the same slug that names the transcript directory, so `General Voice` and `general-voice` are the same channel, and **the key you write is always exactly the directory the summaries land in**.

#### Per-channel settings

| Setting | Default | Purpose |
|---|---|---|
| `channel` | — | Text channel to post in, by name. Unset writes to disk and posts nothing |
| `prompt` | `recap` | Which prompt summarizes a sealed session |
| `retelling_prompt` | `bard` | Which prompt turns a stored summary into something to say out loud |
| `retelling_words` | `200` | Roughly how long the spoken retelling should be — a target the prompt is told to aim at, not a cap it is cut to. About a minute out loud |
| `minimum_utterances` | `5` | Below this a session is not a conversation and is not summarized |
| `backoff_seconds` | `120` | How soon the channel can be told the same evening again. `0`, or below, tells it every time |
| `session_gap_minutes` | `10` | How long the room can sit quiet before the rest of the night is a different evening. Not `resume_seconds`, and not to be set to match it |
| `schedule` | *(unset)* | When a session in this room may **start** being written down, as a list of `Wed 17:00-00:00`. Unset keeps every session, or whatever `settings.transcripts.schedule` says |
| `preamble` | `Sure! Let me go look at my notes.` | What plays while the model is thinking |
| `empty` | `I don't have any notes from this channel yet.` | What plays when nothing has ever been written down in this room |
| `missing` | `I don't have any notes from then.` | What plays when there are notes, just not from the evening that was named |
| `closing` | — | A fixed line played once the story is told. Unset, and the retelling prompt's own sign-off is what says it finished |
| `hold_music` | — | A WAV in `SPEECH_DIR/chimes`, named without its `.wav`, looped under the wait once the preamble runs out. Unset leaves the wait silent |
| `hold_volume` | `0.15` | How loud that music is next to `PLAYBACK_VOLUME` — `0.15` is 15% as loud, not 15% of the amplitude ([how a volume is read](#volumes)). Clamped to `0`–`1` |
| `name` | `miss quote`, `misquote`, `missquote`, `mis quote`, `ms quote`, `mizquote` | What the bot answers to, in the spellings a transcriber returns for a name it has never been told. **Replaces** the default |
| `triggers` | `what happened`, `what did we do`, `recap`, `read me your notes`, `tell me about` | How asking **starts**; which evening is a clause after it. **Replaces** the default |

#### Writing it down

When a session seals — after the resume window, or immediately on shutdown — the JSONL is reduced to the two fields a summarizer wants:

```
Erik: that should work
Eli: it did not
```

`user_id` goes because a model cannot look anybody up by it. The timestamp goes because the lines are already in the order they were spoken and every prompt says so. Consecutive lines from one speaker are joined, because the segmenter cuts on a pause rather than on a sentence and three attributions in a row reads as an exchange that never happened.

That goes to the endpoint with the channel's `prompt`, and what comes back is written to `SUMMARY_DIR` and posted to the text channel named in `channel:` — by **name**, resolved against the server when it is posted. The cost is that a renamed channel silently stops receiving posts, which is why an unresolvable one is reported at startup rather than at the end of the first session worth keeping. Leaving `channel:` out writes summaries to disk and posts nothing, which is a whole working configuration for a server that only wants the spoken recap.

A session under `minimum_utterances` is not summarized: a summary of four lines is longer than the four lines. **A failure anywhere costs the summary and nothing else** — nothing partial is written and nothing partial is posted, and the transcript is untouched, so a session missed because the endpoint was down can be summarized by hand later.

**A whole session is sent in one request, and it is not truncated.** A long evening is tens of thousands of tokens, and an endpoint whose context will not take it refuses the request — which is a failure like any other: logged, no file, no post, transcript intact. Silently cutting a transcript would produce a summary that reads as complete and covers the first hour, which is worse than not having one. Point `LLM_MODEL` at something with the context to hold a session.

> **On shutdown.** A session sealed as the pod goes down is summarized inside the shutdown, before the gateway connection closes, so a whole LLM round trip runs inside the termination grace period and can be killed by it. That is accepted — the transcript survives and the summary is the derived artifact — but it is why [`settings.llm.timeout_seconds`](#settings-llm) should stay well under `terminationGracePeriodSeconds`.

#### Reading it back

**"Miss Quote, what happened last session"** and the bot tells you, out loud, having run the stored summary through a second prompt that turns a thing you read into a thing you say.

It answers **for that channel**, with the whole of the evening asked about. A session still in progress has no summary yet, so this is the previous conversation even when it is asked for in the middle of one, which is exactly what "last session" means. What counts as one evening is [a run of sessions]({{ '/about/#one-evening-is-not-always-one-session' | relative_url }}), not one file.

Asking takes **both** a name and a trigger, the name first, in one breath. An unaddressed "what happened last session" is somebody talking to the room, and answering it would be a minute of narration nobody asked for. Punctuation is ignored on both sides, and several spellings of the name ship by default, because an ASR guesses phonetically at a name it has never been told and "Miss Quote" comes back as one word about as often as two.

A trigger is the **start** of a question rather than the whole of one, and what follows it says which evening:

| Said after a trigger | Means |
|---|---|
| nothing, `last time`, `last session`, `last night`, `last one` | The most recent evening |
| `last week`, `a week ago`, `two weeks ago` … `eight weeks ago` | The evening nearest that date, within three days either way |
| `on the twelfth`, `the twenty fifth`, `the 12th` | That day exactly |

The ordinals are spelled out because that is what comes back: a transcriber writes "the twenty fifth" for a spoken date, not "the 25th". Digits are understood too, for the one that does, but a **bare** number is not a date — "recap the three things" is a request about something else.

A named day is read as one of the last two months: earlier this month if it has already been, and the month before if it has not. Today counts as "has not", since a day that has not finished is not an evening anybody has notes from yet. A day the resolved month does not have gets the `missing` line rather than sliding to a neighbouring date nobody named.

Counting back weeks gets a few days of latitude because a channel that meets on a night of the week does not meet on a date; a tie between two equally close evenings goes to the later one. Naming a day gets none. A day with two conversations on it resolves to the later.

This is also why the trigger list is short. `what happened` covers every row of that table, so there is no line per date anybody might name — and because the stems no longer carry a date, a trigger has to be followed by one of those clauses **or by nothing at all**. That is what keeps "Miss Quote, what happened to my beer" from being a question about last Thursday.

The part worth explaining is the silence. Inference takes seconds, so the bot plays a pre-rendered *"Sure! Let me go look at my notes."* — and **starts the inference before it starts saying it**, so the announcement covers the wait rather than being followed by one. The lookup happens first, so the bot never announces that it is going to look and then finds nothing; with nothing to find it says the `empty` line — or the `missing` one, if the trouble is the night that was named — and stops.

A second ask while a retelling is still going is dropped rather than queued, and `backoff_seconds` is how soon after one the channel can be told it again. The window is per **evening**, not per channel: what it exists to stop is the same story twice, and somebody asking about a different night is asking a second question with a different answer.

**The retelling itself is never cached.** The account of one evening is composed for one moment and nobody will ever ask for those exact words again. It is synthesized, played, and let go — the preamble, the empty line, and a `closing` if there is one are the ones worth keeping, and those are.

#### Hold music

The preamble covers a couple of seconds. A completion routinely runs longer, and what is left over is dead air. `hold_music` fills that.

It is **off unless a channel names a clip**, and nothing is shipped — the audio is yours. Drop a 16-bit WAV in `SPEECH_DIR/chimes` beside the flourishes and name it here without its extension. It **loops**, so what belongs there is a short passage that meets itself rather than a track; and since a clip is read into memory whole and held for the life of the process, ten to thirty seconds is the size to author, not three minutes.

- **It fades up** over [`settings.tts.hold_fade_in_ms`](#settings-tts) (500 ms) as soon as the preamble ends. Quickly, because the gap it is covering has already started.
- **It loops for as long as the wait lasts** — the model thinking, and then the synthesizer starting on the answer. Both are covered: a completion that returns instantly still leaves the `lead_ms` head start to be waited out.
- **It fades down** over `settings.tts.hold_fade_out_ms` (2 s), starting only once the first speech is in hand, so the music reaches silence exactly where the first word begins rather than being cut off at it.

The music and the retelling are **one clip**, armed once — two calls would put a gap exactly where this is trying not to have one. `hold_volume` says how loud the music is next to `PLAYBACK_VOLUME` and applies to the music alone; the retelling arrives at the loudness it would have had anyway. A clip that is missing, or will not parse, costs the music and not the answer. The name is checked at startup and reported if it is not there, and kept regardless, so a volume mounted later starts working without a restart.

**The story ends itself.** A retelling runs to a minute and ends wherever the model chose to end it, so a channel that has been listening has no way to tell "finished" from "stopped". `bard` is told to close on a line that means the tale is over, in the voice it has been telling it in. `closing` is the other way to do it: a fixed sentence, played after the story. It is **unset by default**, since a fixed line following one that has just said goodbye is one goodbye too many.

#### Prompts

Prompts are named and selected by name. Three ship, as prose in `src/miss_quote/resources/prompts.yaml` rather than as strings in the source — a prompt is content, and the file also says which one does each job by default:

| Name | For | Output goes to |
|---|---|---|
| `recap` | The default. An account of the evening for the people who were there, in the order it happened, naming names | A Discord message, so Markdown is fine |
| `minutes` | Topics, decisions, and open questions, as headed sections | A Discord message |
| `bard` | The default retelling. A bard telling the room its own evening back, in the third person, cut down to what actually mattered and signed off so the room knows it ended | **A speech synthesizer**, so it forbids Markdown, bullets, and emoji at some length — a synthesizer reads an asterisk out as a word |

`prompts:` adds your own to those, and one written under a shipped name replaces it — which is how a server that likes the structure of `recap` and not its tone changes the tone without inventing a name for it. It sits at the tool level rather than inside a channel because a prompt is a library entry, and restating a paragraph of instructions once per room is how two of them end up saying different things by accident.

A prompt of your own can pull in the text the shipped ones share by naming it in braces:

| Fragment | What it is |
|---|---|
| `{transcript_instructions}` | The paragraph describing the script format. Any prompt summarizing a session wants it; no retelling prompt should carry it, a retelling being given the summary `recap` already wrote |
| `{retelling_instructions}` | Says that an evening can arrive as several accounts set end to end, and is to be told as one story |
| `{retelling_closing}` | The instruction to end on a line that means the story is over |
| `{words}` | Filled per channel from `retelling_words`. Cannot be used as a fragment name |

Any other braces are left exactly as written, so an example of the JSON you want back survives. A shipped prompt naming a fragment that does not exist stops the bot at startup; one of your own is left alone, since braces in it are usually deliberate.

**A prompt named by a name nothing answers to stops the tool from starting**, reported alongside every other startup problem. A tool running on instructions nobody asked for produces summaries that look fine and are not what the file requested, which is worse than a tool that refuses.

### scoreboard {#scoreboard}

Keeps a running balance per person, writes it down, and puts the standings under the name of whatever voice channel the bot is in. It hears nothing and says nothing out loud; what it does is count for the tools that ask it to.

```yaml
scoreboard:
  enabled: true
```

There is nothing to configure per server. What the tally is counted in and how often it is written and published are [`settings.credits`](#settings-credits), and where it lives is `CREDITS_FILE`, because there is one file behind every server's board and how often it is written is a property of the file rather than of any one server.

**It is enabled separately from whatever is counting.** A server that wants fines announced but not tallied enables `verbal-morality` and not this; the fines are announced and nothing is kept, and the log says so once at startup rather than leaving it to be discovered by wondering why the channel topic is empty.

**The standings go in the voice channel topic**, as `Eli: -9 Erik: -2 Luke: -1 Ryan: 0`, which makes the topic the scoreboard — visible without asking the bot anything. They go up as soon as the bot takes up a channel and are kept current for as long as it sits there; a channel the bot leaves keeps the last board it was shown. A fine is a **debit**: everybody starts at nothing and goes down, so the number beside a name reads as what swearing has cost them rather than as points collected. Nothing assumes that direction, and `quotes` calls `credit` to pay for a title named in time, so a balance can climb back toward nothing and past it.

The board holds the **four furthest into the red, worst first**. A leaderboard rearranges itself every time somebody passes somebody else, which is the objection to publishing a whole roster in name order; at four places it is short enough to read at a glance. Ties break on the name, so two people on the same balance do not swap places between one edit and the next for no reason anybody can see.

**Only `users` are eligible for the board.** Everyone on the roster starts on it at nothing spent, so a channel says who is being watched before anybody has sworn. Somebody the server never wrote down is still heard, still announced, and still counted under whatever Discord reports — they are simply not published, because a display name its owner can set to anything is not something to put in a channel topic. A server with no roster at all publishes nothing rather than an empty line, since setting the status to nothing would wipe whatever a person had put there.

Counts are **per server**. The same person swearing in two servers owes two separate debts. Identity is the user ID and the name is only what gets printed, so a rename does not hand somebody else's debt to whoever inherited their nickname.

The tally is kept in `CREDITS_FILE` and **loaded at startup**, so a restart is not an amnesty. It is written back on the same interval it is published on, and again on shutdown — the shutdown pass writes the file but does not touch the topic, because a channel edit waiting out a rate limit would sit on `SIGTERM` until the pod was killed outright. A file that will not parse is reported and ignored rather than raised on.

What it actually sets is the channel's **status**, not its topic. A voice channel has no topic: `PATCH /channels/{id}` with one is refused with `CHANNEL_TOPIC_INVALID`, *"Field contains at least one word that is not allowed"* — which reads like a profanity filter and is nothing of the kind, since it refuses a topic of `test` identically. It needs **Set Voice Channel Status** on the channel — not Manage Channels — and without it the tool logs once per change and keeps counting.

The **status is not set on every change.** Both the write and the edit are driven off a revision counter, so a tally that changed four times between two ticks costs one of each. They run on **separate intervals**, because they are limited by different things: writing a few hundred bytes is cheap, while a status edit is rate-limited. Saving still happens first on every tick, so a pod terminated mid-edit still has the tally on disk from the tick before.

A request Discord **refuses** — a `400`, or a missing permission — is not retried, because retrying it every interval would spend the channel's rate limit on an answer that cannot change. A tally that then changes is published anyway, since what was refused was that text and the next text is not that text. Every failure is logged with the string it was trying to set.

### tts {#tts-tool}

Says things out loud, and is the only thing that plays anything. It hears nothing and decides nothing; what it does is own the rendered-speech cache, the chime library, the volume, and the voice connection, so that everything a channel hears arrives by one route.

```yaml
tts:
  enabled: true
```

There is nothing to configure per server. Which synthesizer, which voice, how long a clip is kept and how much of one is held back before playback are [`settings.tts`](#settings-tts) and `TTS_HOST` / `TTS_PORT` / `TTS_VOICE`, because there is one synthesizer behind every server. All this setting says is whether **this** server is allowed to speak through it.

**It is enabled separately from whatever is talking.** A server that enables `verbal-morality` and not this counts fines and says nothing; one that enables `quotes` and not this runs its rounds, pays them, and answers nobody. Both are said once at startup rather than left to be discovered by wondering why the channel is quiet.

**Other tools speak through it.** `play` is the whole interface, and `play_held` is `play` for a sentence that does not exist yet — a task is handed over rather than awaited, and the hold music and the answer come out as one clip. `chime` names a WAV in `SPEECH_DIR/chimes` **without its extension**, played ahead of the words; a chime that is missing costs the chime and not the announcement.

**How a clip reaches Discord is decided here, and it is the difference between free and not.** A phrase with nothing in front of it and nothing to be done to it is handed over exactly as it was stored — Opus packets, no decode, no encode, no resample. A chime, or any volume below the channel's own, means samples. So `quotes`, which never uses a chime and never turns itself down, takes the free path every time, and a backed-off fine with a flourish in front of it does not. The whole of that path is described under [Speech]({{ '/about/#speech' | relative_url }}).

**Rendering in advance is its `run`.** A tool that can work out at startup what it will have to say hands over the list, and this renders it in the background while the bot is already in the channel, one phrase at a time across the whole process. A phrase already queued is dropped; a phrase that will not synthesize is a line in the log and then the next phrase, never the end of the run.

### verbal-morality {#verbal-morality}

The Verbal Morality Bot, after *Demolition Man*. It listens for words the server has decided against and, on hearing one, announces the fine out loud in the channel it was said in. The credits are imaginary but they are counted, by somebody else: the fine is handed to the server's [`scoreboard`](#scoreboard). **With no `scoreboard` enabled the fine is announced and not counted**, which the log says once at startup.

```yaml
verbal-morality:
  enabled: true
  config:
    words: [fiddlestick, poppycock]
    announcement: "{user}, you are fined {credits} for {violations} of the verbal morality statute."
    repeat_announcement: "{user}, you are also fined {credits} for {violations} of the verbal morality statute."
    chime: chime
```

| Setting | Required | Purpose |
|---|---|---|
| `words` | yes | Stems of what the server objects to. A lone one may be written unquoted rather than as a list |
| `announcement` | no | What gets said. `{user}`, `{credits}`, and `{violations}` are the placeholders |
| `repeat_announcement` | no | Said instead when the same speaker is fined again inside [`settings.fines.repeat_seconds`](#settings-fines). Same placeholders |
| `chime` | no | A WAV in `SPEECH_DIR/chimes`, played ahead of the announcement, named without its `.wav` |

Both templates default to the lines above, which the tool carries, so a server that wants the defaults can leave them out. A template with a placeholder nothing fills is rejected at startup rather than at the moment someone swears, and the error names which of the two it was.

#### Words are stems

Each is expanded once at startup into the endings it is said with — a plural, a past tense, a gerund with and without its `g`, someone who does it, something that is like it, and the three that make it a noun again — so `fiddlestick` also catches `fiddlesticks`, `fiddlesticked`, `fiddlesticking`, `fiddlestickin`, `fiddlesticker`, `fiddlestickers`, `fiddlesticky`, `fiddlestickity`, `fiddlestickery`, and `fiddlestickiness`. A list that has to spell out every ending is a list somebody gets around a week after writing it.

Expansion is English spelling rather than a dictionary: a final consonant doubles after a short vowel (`shit` grows a `shitter`, not a `shiter`), a silent `e` drops before a vowel, a sibilant takes `es`, and a `y` after a consonant becomes an `i` — except before an ending that already starts with one, so it is `shittiness` and not `shittyiness`.

Doubling really turns on where the **stress** falls, and nothing here knows that, so the syllable count stands in for it — right for the single-syllable words this is mostly pointed at, and wrong for a **compound**, which keeps the stress of the word it ends with. `dipshit` is two syllables and still takes `dipshitting`. Nothing structural separates that from `bugger`, which splits the same way and must stay `buggering`, so the words that carry their doubling into a compound are named in `COMPOUND_ENDINGS` in `utils/stems.py`. If a list grows a compound that conjugates wrong, that is the one line to add to.

Nothing checks whether the result is a word anybody says, and it does not need to — a form nobody utters costs a few bytes in an alternation, while a missing one costs the tool the thing it exists to catch. Note that expansion can reach a word that is innocent on its own; **a stem whose endings collide with ordinary speech is worth checking before it goes in the list.**

Matching is **whole words, case-insensitive**. A substring match fines the innocent, and the canonical example, Scunthorpe, is a place people live.

#### What a fine costs, and how loudly

**The fine scales with the utterance**: one credit per forbidden word in it, so three of them is `3 credits` and one is `1 credit`. The count is filled into `{credits}` already pluralized, as a numeral — every synthesizer worth pointing this at reads `3` as a number, and `1 credits` is wrong in a way a listener hears. What a credit is *called* is [`settings.credits.currency`](#settings-credits), and the plural is grown from it by the same spelling rules the word list uses, so `penny` announces as `2 pennies`. `{violations}` agrees with the count, reading `a violation` for one and `multiple violations` for more.

What does not scale is the number of announcements. Three violations in one utterance earn one, because three announcements over the top of each other is a denial of service on the channel. **A violation earned while an announcement is playing is counted and not announced at all** — the speaker plays one clip at a time, so the alternative is a queue, and a channel where three people swear over each other would spend the next minute being read fines for things it has moved on from. The tally is charged either way: what somebody owes is not a function of whether they were told about it.

**Being fined twice in a row is worded differently.** A speaker fined again inside `settings.fines.repeat_seconds` gets `repeat_announcement` — "you are *also* fined" — because reading the whole sentence out again sounds like a bot that has lost track of what it just said. It is per speaker: somebody else swearing in the meantime does not make their first fine a repeat.

**A repeat offender is announced more quietly.** Being fined is the joke, and the joke told fifteen times in five minutes is a denial of service on the conversation. Every violation inside a sliding `settings.fines.backoff_seconds` takes `settings.fines.backoff_percent` off the next announcement, down to `settings.fines.volume_floor` — at the defaults, 5% a violation over five minutes, floored at a quarter as loud as `PLAYBACK_VOLUME`, so fifteen of them reach the bottom. The percentage is off what a listener hears rather than off the amplitude ([how a volume is read](#volumes)), so each step is one somebody can actually notice. The first swear in a window is announced at full volume: the backoff is for saying it again. The window is per speaker and per server, held in memory only. What it does **not** affect is the tally.

**The announcements are rendered at startup.** The roster is known before anybody speaks and so is the shape of the sentence, so on the way up the tool hands `tts` every name in `users` against one, two, and three violations, in both wordings. Three violations because that is what a sentence usually holds; a fourth is remarkable enough to wait for the synthesizer. What cannot be warmed is anyone **not** on the roster: they pay for their first fine and nobody pays for it again.

`chime` is resolved **inside** `SPEECH_DIR/chimes` — a bare name, or a path below it; anything that climbs out is refused at startup. It must be a **16-bit WAV**, at any sample rate and in mono or stereo, both of which are converted on the way in. WAV rather than MP3 because playing audio without ffmpeg is the point of this path, and nothing in the image can decode anything else.

A server electing in with no `words` is enabled and listening for nothing, which is reported at startup rather than left to be discovered by swearing at it.

## Settings

The `settings:` block of `config.yaml`. Every one of these has a default, so none of them has to be written down; a name or a value that will not parse is reported at startup and falls back to the default.

### settings.tts {#settings-tts}

Only used by tools that answer out loud. Where the synthesizer *is* is `TTS_HOST` and `TTS_PORT`, and which voice it uses is `TTS_VOICE`.

| Setting | Default | Purpose |
|---|---|---|
| `timeout_seconds` | `30.0` | Budget for a **single** wait on the synthesizer, not for a whole clip — a long phrase arriving steadily is not cut off for taking a long time |
| `stall_seconds` | `10.0` | How long the player waits mid-clip for audio that never comes before ending it |
| `lead_ms` | `500.0` | How much speech to have in hand before a clip starts playing, so a synthesizer that renders a phrase whole leaves no gap behind a chime. `0` starts on the first chunk |
| `hold_fade_in_ms` | `500.0` | How quickly music under a wait arrives. Only the `summary` tool asks for any, and only for a channel that set `hold_music`. `0` is a cut |
| `hold_fade_out_ms` | `2000.0` | How slowly it leaves, timed to reach silence where the first word starts. `0` is a cut |
| `cache_retention_days` | `90` | Days anything in `SPEECH_DIR/cache` survives without being played, counted from the last time it was. Also what clears out clips an earlier version wrote as WAVs, and `.partial` files orphaned by a process killed mid-write. Any value below `1` keeps them forever. Chimes live elsewhere and are never reaped |

### settings.credits {#settings-credits}

Only used by `scoreboard`. Where the tally is written down is `CREDITS_FILE`.

| Setting | Default | Purpose |
|---|---|---|
| `currency` | `credit` | What a balance is denominated in, in the singular. The plural is grown from it by the spelling, so `penny` announces as `2 pennies`. Wording only — it changes nothing about what is counted |
| `save_seconds` | `5.0` | How often a changed tally is written to disk. `0`, or any value below it, stops the loop: the tally is kept in memory and written only on shutdown |
| `topic_seconds` | `10.0` | How often a changed tally is published to the voice channel topic — set as the channel **status**, a voice channel having no topic. The board also goes up the moment the bot takes up a channel, changed or not, so a fresh room is not left blank until somebody swears. `0`, or any value below it, keeps the tally off the channel entirely |

### settings.fines {#settings-fines}

Only used by `verbal-morality`. What a fine is *worth* is the scoreboard's; these are how it is said.

| Setting | Default | Purpose |
|---|---|---|
| `repeat_seconds` | `5.0` | How soon after being fined the same speaker is told they are "also fined" rather than hearing the whole sentence again. `0`, or any value below it, turns the second wording off |
| `backoff_seconds` | `300.0` | The sliding window a violation counts for against how loudly the next one is announced |
| `backoff_percent` | `5` | How much of the next announcement's loudness each violation inside that window takes off, and off the knob rather than the amplitude, so 5% is 5% quieter to listen to ([how a volume is read](#volumes)). `0` takes nothing off, turning the backoff off; anything above `100` reaches the floor on the first repeat, and anything negative is treated as `0` rather than made louder |
| `volume_floor` | `0.25` | The quietest a fine is announced once a speaker has earned the full backoff, as how loud it is next to `PLAYBACK_VOLUME` — a quarter is a quarter as loud ([how a volume is read](#volumes)). `0` silences a repeat offender entirely; `1` turns the backoff off |

### settings.quotes {#settings-quotes}

Only used by `quotes`. The triggers and the lines themselves are a YAML file at `QUOTES_FILE`, plus whatever a server [added for itself](#what-a-server-adds-for-itself).

| Setting | Default | Purpose |
|---|---|---|
| `backoff_seconds` | `300.0` | How long a trigger stays spent after it fires, so a channel that keeps saying the same word hears the line once. `0`, or any value below it, answers every trigger every time |

### settings.transcripts {#transcripts}

Where transcripts are written is `TRANSCRIPT_DIR`, and what clock they are stamped with is `TZ`.

| Setting | Default | Purpose |
|---|---|---|
| `retention_days` | `-1` | Days to keep. `-1`, or any value below `1`, keeps forever |
| `resume_seconds` | `5.0` | How long a transcript is held open for a reconnect to the same channel. `0` seals it on disconnect |
| `schedule` | *(unset)* | The default windows for a room listed in `monitored_channels` that names none of its own, as a list of `Wed 17:00-00:00`. **Not** what decides which rooms are kept — that is the room list itself. See [the capture schedule]({{ '/about/#the-capture-schedule' | relative_url }}) |

Pruning is **off by default**. Any value below `1` disables it entirely, so `0` is a no-op rather than "delete everything" and a mis-set setting cannot destroy the archive. When set to a positive `N`, files older than `N` days are deleted, aged by the **date at the front of the filename** rather than mtime — the filename is the authoritative record of when a transcript was taken, while mtime misjudges a file appended to late or restored from backup. Pruning runs at startup and whenever a session opens.

### settings.presence {#settings-presence}

What the bot says about itself while a conversation is being kept. Per deployment and necessarily so: Discord has one presence per bot rather than one per server. See [the status]({{ '/about/#the-status' | relative_url }}).

| Setting | Default | Purpose |
|---|---|---|
| `transcribing` | `🎙️ transcribing...` | Shown under the bot's name while any session is on the record, and cleared when none is. Empty turns the signal off. The emoji goes in the words — a custom status has an emoji field of its own, and Discord does not apply it for a bot |

### settings.llm {#settings-llm}

Only used by `summary`. Where the endpoint *is*, what key it wants, and which model to ask for are `LLM_API_BASE`, `LLM_API_KEY`, and `LLM_MODEL`.

| Setting | Default | Purpose |
|---|---|---|
| `timeout_seconds` | `120.0` | Budget for one completion, end to end. Generous next to the ASR's, a summary being several hundred tokens of output rather than a sentence. Keep it well under the deployment's termination grace period |
| `max_output_tokens` | `1024` | A ceiling on what is **generated**. Not the context window and not the whole request: the input is not counted against it. Named for what it bounds rather than for the wire field it becomes (`max_tokens`), whose name has cost more than one person an afternoon |
| `temperature` | `0.7` | How much licence the model has. Higher than a mechanical transform would want, because the output is prose somebody reads for pleasure |
| `thinking` | `true` | Whether a model that reasons before answering is allowed to. `false` sends `chat_template_kwargs.enable_thinking`, and is sent **only** to turn reasoning off — an endpoint that has never heard of the field is never shown it |

#### On reasoning models

Worth knowing before pointing this at one, because the failure is confusing and the fix is a number.

**Reasoning is generated, so it spends `max_output_tokens`.** A model that thinks before it answers puts the thinking in `reasoning_content` and the answer in `content`, and both come out of the same budget. Run out mid-thought and `content` is empty — a `200` carrying nothing, which reads like a broken endpoint and is a setting. Measured against a 27B reasoning model on a real 1,653-line session: 4,137 generated tokens, of which the answer was about 700. At `1024` it never reached the answer at all.

The client says which of those happened rather than making you find out:

```
the model spent its whole 1024-token budget reasoning and never began the
answer. Raise 'settings.llm.max_output_tokens', or set
'settings.llm.thinking: false' to stop it reasoning at all
```

**Reasoning is also most of the wall clock**, which matters in exactly one place. The same session took 94s with reasoning and 12s without, for summaries of comparable quality. Nobody is waiting on a summary written after everyone has left. Somebody *is* waiting on the retelling — they asked out loud and the preamble covers a few seconds, not ninety — so a deployment pointing at a reasoning model will want `thinking: false`, or a model that does not reason, for the sake of that one path.

**Thinking is stripped whichever way it arrives.** Where a model puts it is a property of the serving stack rather than of the model: beside the answer in a `reasoning_content` field, or inline at the front of `content`, fenced in `<think>` tags. The first costs nothing to ignore — only `content` is ever read. The second is cut out, in every spelling seen in the wild (`<think>`, `<thinking>`, `<reasoning>`, `<thought>`, any casing, with attributes, several blocks), because left in it opens the summary with the model talking to itself and the synthesizer reads the tags out loud. An opening tag with no closing partner is a model cut off mid-thought, so everything after it goes too. That happens **whether or not `thinking: false` is set**, because the setting is a request and not a guarantee.

### settings.summaries {#settings-summaries}

Where summaries are written is `SUMMARY_DIR`.

| Setting | Default | Purpose |
|---|---|---|
| `retention_days` | `-1` | Days to keep. `-1`, or any value below `1`, keeps forever. Its own clock, separate from the transcripts': keeping summaries for a year and transcripts for a month is a reasonable thing to want |

## Environment

What a deployment points at, rather than how it behaves. `.env` is loaded if present. Nothing about a particular deployment is baked into the image, so the same image runs anywhere the variables below point it at.

| Variable | Default | Purpose |
|---|---|---|
| `CONFIG_FILE` | `/config/config.yaml` | The mounted file holding `settings` and `servers` |

### Discord {#env-discord}

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token. **Required** — the bot exits immediately without it |
| `COMMAND_PREFIX` | `!` | Prefix for the `!join` / `!leave` commands |
| `AUTOJOIN` | `true` | Join when a human enters a voice channel; leave when it empties. Accepts `true/false`, `1/0`, `yes/no`, `on/off` |

With `AUTOJOIN` enabled the bot connects as soon as a non-bot member enters a voice channel, and disconnects once the channel empties of humans. A bot can occupy only one voice channel per guild, so if a second channel becomes active it stays where it is rather than hopping, which would fragment both transcripts. The `!join` and `!leave` commands remain available either way, and require **Message Content Intent** in the Discord Developer Portal.

### ASR {#env-asr}

| Variable | Default | Purpose |
|---|---|---|
| `WYOMING_HOST` | `localhost` | Hostname or service name of the Wyoming ASR server |
| `WYOMING_PORT` | `10300` | Wyoming's conventional port |
| `STT_LANGUAGE` | `en` | Sent as `Transcribe.language` |
| `MAX_CONCURRENT_TRANSCRIPTIONS` | `4` | Ceiling on in-flight utterances, so a busy channel cannot open unbounded connections against a shared ASR |

### TTS {#env-tts}

Only used by tools that answer out loud. A deployment with no such tool enabled never opens a connection.

| Variable | Default | Purpose |
|---|---|---|
| `TTS_HOST` | `localhost` | Hostname or service name of the Wyoming TTS server |
| `TTS_PORT` | `10200` | Wyoming's conventional TTS port |
| `TTS_VOICE` | — | Voice to ask for. Empty takes whatever the synthesizer considers its default, so a server with one voice loaded needs no setting |
| `PLAYBACK_VOLUME` | `1.0` | How loud everything played into a channel is, chime included. `1.0` is however loud the synthesizer rendered it, `0.8` is 20% quieter, `1.2` is 20% louder and clipped rather than wrapped. A knob rather than a multiplier — see [How a volume is read](#volumes). Any value below `0` is treated as silence. **Below `1.0` every clip is decoded and re-encoded on its way past**, so it has a CPU cost as well as a loudness one — turn a channel down at the Discord end where you can |
| `SPEECH_DIR` | `/speech` | Audio on disk, as one root with a directory per kind. `cache/` is rendered speech as Ogg Opus, written and reaped by the bot — mount a writable volume here, since without one every phrase is synthesized again every time it is said. `chimes/` is where you put a WAV by hand |

#### How a volume is read {#volumes}

Every volume in this bot is a knob rather than a multiplier: `1` is full, `0` is silent, and **half is half as loud to listen to**. That covers `PLAYBACK_VOLUME`, `settings.fines.volume_floor`, `settings.fines.backoff_percent`, and each channel's `hold_volume`.

It is worth stating because the obvious implementation gets it wrong. Hearing is logarithmic, so a clip at half the *amplitude* is under 3 dB down and still sounds around four fifths as loud; halving what somebody actually hears takes about 10 dB. The setting is converted on that curve on its way to the samples, which is why the numbers below do what they say:

| Setting | Amplitude | Attenuation |
|---|---|---|
| `1.0` | `1.0` | 0 dB |
| `0.8` | `0.690` | −3.2 dB |
| `0.5` | `0.316` | −10 dB |
| `0.25` | `0.100` | −20 dB |
| `0.15` | `0.043` | −27.4 dB |
| `0` | `0` | silence |

Volumes multiply as knobs, so a tool asking for half of a deployment set to half is announced at a quarter — a quarter as loud, which is a tenth of the amplitude. Nothing has to be set in decibels, and a value from a deployment that predates this reads about as it used to at the loud end and considerably quieter below halfway.

### Quotes {#env-quotes}

| Variable | Default | Purpose |
|---|---|---|
| `QUOTES_FILE` | `/app/src/miss_quote/resources/quotes.yaml` | The triggers and the lines they answer with, as a YAML mapping of title to trigger to line. The deployment's list; the image ships the one in `resources/`, and mounting a file over that path replaces it. A path and only a path; a server that wants a list fetched over HTTP says so under [`additional_quotes`](#keeping-that-list-somewhere-else) |

### Credits {#env-credits}

| Variable | Default | Purpose |
|---|---|---|
| `CREDITS_FILE` | `/credits/credits.json` | The running tally, as JSON. One file behind every server's board. Mount a volume at its directory to keep what everybody owes across restarts |

### LLM {#env-llm}

Only used by `summary`. An OpenAI-compatible chat-completions endpoint and nothing more specific than that: a root, an optional bearer token, and a model name. `/chat/completions` is the whole of the API surface used, which is the part every endpoint claiming compatibility actually implements — so a hosted API, a gateway in front of one, and a model on the next machine over are the same three variables.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_BASE` | `http://localhost:8080/v1` | The API root, with `/chat/completions` appended. There is no default that will work out of the box, in the same way there is none for the ASR |
| `LLM_API_KEY` | — | Sent as a bearer token when there is one. Empty sends **no `Authorization` header at all**, rather than an empty credential for an endpoint to decide what to do with. Never logged and never in an error message |
| `LLM_MODEL` | — | What to ask for. **Required** by `summary`; there is no default, a model name being a deployment's own and a guess being a 404 that reads like a broken endpoint |

### Transcripts {#env-transcripts}

| Variable | Default | Purpose |
|---|---|---|
| `TRANSCRIPT_DIR` | `/transcripts` | Directory the session files are written to |
| `SUMMARY_DIR` | `/summaries` | Directory the summaries are written to, in a tree the same shape as the transcripts'. A separate root so the two can be mounted and shared on different terms |
| `TZ` | `America/Los_Angeles` | Timezone for session filenames and the offset stamped on each line |

### Speech segmentation {#env-segmentation}

| Variable | Default | Purpose |
|---|---|---|
| `SPEECH_FLUSH_TIMEOUT_SECONDS` | `2.0` | Transcribe a speech buffer that stopped receiving audio, e.g. a speaker who muted mid-sentence |
| `USER_TIMEOUT_SECONDS` | `60` | Discard per-user VAD state after this much silence |
| `LOG_LEVEL` | `INFO` | Standard Python log levels |

VAD thresholds, the pre-roll depth, and the Wyoming chunk size are deliberately **not** configurable at all, from either place — they are tied to Silero's fixed 512-sample frame and live in `config.py`.

<nav class="page-nav" aria-label="Previous and next page">
  <a class="page-nav-prev" rel="prev" href="{{ '/installation/' | relative_url }}"><span class="page-nav-label">← Previous</span><strong>Installation</strong><span class="page-nav-blurb">Volumes, permissions, and deploying it</span></a>
</nav>
