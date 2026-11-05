# beloura-hire

An automated recruitment assistant and self-serve chat assessment for hiring chat operators, built with FastAPI.

A single assistant ("James") handles intake, screens applicants, runs a short text interview, and talks to each applicant on Telegram, Discord or email. Applicants then take a self-serve assessment — a continuous, chunked chat with an LLM-simulated fan plus two short "curveball" scenarios — which an LLM judge scores (persona, rapport, sales, pacing, boundaries) with behavioural typing/AI-use signals. A small team grades the saved chats to calibrate the judge.

## Stack
- FastAPI + SQLite (WAL) + Jinja2 templates, one background worker thread
- LLMs via an OpenRouter-style gateway (no SDK, plain httpx); every call is logged with cost
- Telegram Bot API, Discord (discord.py), SMTP/IMAP email — each optional
- Packaged as a container with an optional Cloudflare tunnel sidecar

Any integration with an empty token runs in **dry-run** (no external calls), which is how the test suite runs.

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in what you need; leave tokens empty for dry-run
HIRE_NO_WORKER=1 .venv/bin/python -m unittest discover -s tests      # 160 tests
HIRE_NO_WORKER=1 PANEL_PASSWORD=x .venv/bin/uvicorn app:app --port 8140
# then: /healthz, /apply/<channel>, /staff (Basic auth admin:x)
```

Or with Docker: `docker compose up --build` (reads `./.env`).

## Layout
- `app.py` routes, auth middleware, apply form, Telegram webhook, worker thread
- `engine.py` state machine (screening, scoring, stages, scheduling, reminders)
- `sam.py` the assistant loop (tool-calling over the LLM gateway)
- `interview.py` the five-question text interview
- `assess.py` / `assess_llm.py` / `tools/assess_bank.py` the assessment portal (fan simulator, judge, scoring, grading)
- `channels.py` Telegram / Discord / email adapters
- `posts.py` / `report.py` job-post rendering and a weekly funnel report
- `texts.py` the templated message voice

## Notes
This is an internal tool published for reference. Configuration (keys, hostnames, IDs) is supplied via environment variables; see `.env.example`.
