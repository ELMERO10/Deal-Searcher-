# Deal-Searcher

An automated AI agent that hunts for computer-hardware deals on Newegg, scores them with AI, and emails me only the good ones.

## How it works

The program is a small pipeline of four agents:

1. **Finder** reads deal feeds (r/buildapcsales, DealNews, Slickdeals) and keeps only the posts that mention Newegg.
2. **Evaluator** sends each deal to Google's Gemini API, which scores it against my criteria: real discount versus typical street price, reputable brands, and penalties for refurbished items, padded bundles and accessories.
3. **Presenter** emails the high-scoring deals right away. If email is not set up, results are printed on screen.
4. **Price Watcher** keeps a watchlist of mid-scoring products and alerts me when a repost shows a meaningful price drop.

A local SQLite database remembers which deals were already seen, so I never get the same one twice.

## Tech

- Python
- Google Gemini API (free tier) for deal scoring
- SQLite for storage
- SMTP (Gmail) for email alerts
- Windows Task Scheduler runs it automatically every 6 hours

## Setup (Windows PowerShell)

```powershell
py -m pip install requests google-genai
$env:GEMINI_API_KEY = "your-free-key-from-Google-AI-Studio"

# Optional email alerts (Gmail needs an app password)
$env:SMTP_USER = "you@gmail.com"
$env:SMTP_PASS = "your-app-password"
$env:MAIL_TO = "you@gmail.com"

py newegg_deal_agent.py
```

Settings such as the feeds, the store keyword and the scoring criteria are at the top of the script.

## Why I built it, I built it becuase I wanted to find a laptop and phone that had the best value/price and I didnt had the time to look all day for it so now I got this and made it to also help others when you dont have much time. MADE BY JAFET FLORES. 

I wanted to learn how to build AI agents that work on their own: a program that gathers data, makes judgments, and acts without me supervising it.
