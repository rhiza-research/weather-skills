# Talk track: blending forecasts for farmers (5 minutes, for Bill Gates)

About 700 words, around 140 words a minute. **[Demo]** marks the live run. Numbers come from the run on 21 October 2026; rerun `run_demo.sh` before the meeting and update the bracketed figures if they change.

---

**0:00 — The farmer's bet (45 s)**

For a smallholder farmer in Kenya, the most expensive decision of the year is a date: when to plant. Plant on a false start, and the seed dies in the dry spell that follows. Wait too long, and you lose weeks of growing season you can't get back. The question they need answered is simple: *when will the rains really start where I farm?*

Weather models already forecast rain out to a month ahead. But no single model is reliable enough on its own, and each one is wrong in its own way.

**0:45 — What Chicago showed (45 s)**

A team at the University of Chicago tackled this for India's monsoon. Their idea is to stop picking a favourite model. They take several forecasts, including new AI weather models, and learn from decades of history how much to trust each one, for each week ahead. The blend then gives an honest probability, like "a 30% chance the monsoon starts in your district next week", that is tested against what actually happened. That blending step is what turns raw forecasts into advice.

**1:30 — What we built (45 s)**

We turned that method into a general tool that Claude can run, for any weather event and any place, not just monsoon onset.

Behind it:
- 20 years of past forecasts from the European and US weather centres;
- the Cumulus AI model;
- 45 years of satellite rainfall;
- 330 weather stations from TAHMO across East Africa.

The tool learns the weights from history, checks itself honestly, and then blends today's forecasts.

**2:15 — [Demo] (60 s)**

*Run `bash $HERE/run_demo.sh`. It finishes in about 2 minutes; talk over it.*

"I've asked Claude to forecast this season's rains for Kenya. Right now it is building the record of when the rains actually started in every year since 1981. Then it tests itself: it hides one year at a time and checks whether the blend would have got that year right. Then it fits the blend and applies it to this week's forecasts."

*Open `onset/map_ecmwf+gefs.png`.*

"Each panel is one week ahead. The darker the square, the more likely the rains properly start that week."

**3:15 — What it says (60 s)**

Two things.

First, **it works, and we can prove it.** Tested on 20 past seasons the blend never saw, it beats the "historical average" forecast at every lead time out to four weeks: about [12–15]% better one week ahead and about [10]% four weeks ahead. For a simpler question, *will this week be wet?*, it is about [40]% better one week ahead. It's also the same tool, just asked a different question.

Second, **this season**: from the 15 October forecasts, there's roughly a [4-in-10] chance across Kenya that the rains properly start by mid-November. It's higher around Lake Victoria first, then in central and south-eastern Kenya. This is an El Niño year. In years like it, the rains have tended to come early, around mid-October, so we'll watch this closely as new forecasts arrive.

**4:15 — Why it matters and what's next (45 s)**

What used to take a research team months now runs in minutes, for any crop-critical event:
- the start of the rains;
- a damaging dry spell;
- a flood-risk week.

It gets better as new AI models arrive, because the blend learns how much to trust each one.

Next we want to run it every week with Kenya's meteorological department and ICPAC, check it against farmers' own rain gauges, and get it to farmers' phones, as the Chicago team did in India. To do that we need partners on the ground and long-term access to the models and station data.

---

## If asked

- **"How good is the AI model, Cumulus?"** "Promising: combined with ECMWF it's our best blend one to two weeks ahead. But it only has six seasons of history to test on, and we're confirming with its developers which years it was trained on, so we're not over-claiming."
- **"Why not just use the best single model?"** "The best model changes by lead time and by season. The blend learns that from data instead of us guessing."
- **"Is 12% better a lot?"** "For onset, a whole month ahead, yes. Onset is a rare event in any given week, so beating the historical rate at all is hard. It means farmers get usable odds, not a coin flip."
- **"What does it cost to run?"** "Minutes on a laptop once the data is downloaded. The expensive part, the forecast models themselves, is already run by the weather centres and by Cumulus."
