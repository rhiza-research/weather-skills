# Talk track: blending forecasts for farmers (5 minutes, for Bill Gates)

About 700 words, around 140 words a minute. **[Demo]** marks the live run. Numbers come from the run on 21 October 2026 with the 15 October forecasts. Rerun `run_demo.sh` before the meeting and update the bracketed figures if they change.

---

**0:00 — The farmer's bet (45 s)**

*Start `bash $HERE/run_demo.sh` as you begin speaking. It takes about 2½ minutes, so it's done by the demo section.*

For a smallholder farmer in Kenya or Uganda, the most expensive decision of the year is a date: when to plant. Plant on a false start, and the seed dies in the dry spell that follows. Wait too long, and you lose weeks of growing season you can't get back. The question they need answered is simple: *will the rains really start where I farm this week?*

Weather models already forecast rain a month ahead, as ensembles of dozens of slightly different runs. But no single model is reliable enough on its own, and each one is wrong in its own way.

**0:45 — What Chicago showed (45 s)**

A team at the University of Chicago tackled this for India's monsoon. Their idea is to stop picking a favourite model. They take several forecasts, including new AI weather models, and learn from decades of history how much to trust each one, week by week. The blend gives an honest probability, like "a 30% chance the monsoon starts in your district next week", tested against what actually happened. That blending step is what turns raw forecasts into advice.

**1:30 — What we built (45 s)**

We turned that method into a general tool that Claude can run, for any weather event and any place.

Behind it, for Ethiopia, Kenya and Uganda:
- 20 years of past forecasts from the European weather centre;
- the Cumulus AI model;
- 45 years of satellite rainfall;
- 265 weather stations from TAHMO.

For every past forecast, it asks how many of the ensemble's runs start the rains within one and two weeks. It learns how much that share should be trusted, checks itself honestly, then does the same for today's forecasts.

**2:15 — [Demo] (60 s)**

*Show the terminal: the run printed each stage and its time.*

"While I was talking, Claude worked out this season's rains for Kenya and Uganda. First it found when the rains actually started in every year since 1981. Next it counted, for every past forecast, how many runs got the start right. Then it tested itself: it hid one year at a time and checked whether the blend would have got that year right. Last, it applied the blend to last week's forecasts, and marked where satellite rainfall shows the rains have already begun."

*Open `map_ecmwf+cumulus_Uganda.png`, then `map_ecmwf+cumulus_Kenya.png`.*

"The left panel is one week out, the right panel two. The darker the square, the more likely the rains have properly started by then. The darkest squares have already started: we can see it in the satellite record."

**3:15 — What it says (60 s)**

Two things.

First, **it works, and we can prove it.** Tested on six past seasons it never saw, the blend beats the historical average by about [30]% in both Kenya and Uganda, one week and two weeks ahead. Most of that skill comes from the AI model, Cumulus. On its own, the European model adds about [10]%. The blend learned that from the data; nobody told it.

Second, **this season.** In Uganda the rains have already started across the north and west, and around Lake Victoria the forecast gives high odds they start by 21 October. In Kenya they haven't started anywhere yet. The chance they have by 28 October is about [1 in 5], close to a normal year, highest near Lake Victoria and in the east.

**4:15 — Why it matters and what's next (45 s)**

What used to take a research team months now runs in under three minutes, for any crop-critical event:
- the start of the rains;
- a damaging dry spell;
- a flood-risk week.

It gets better as new AI models arrive, because the blend learns how much to trust each one, and it shows when one earns its place.

Next we want to run it every week with Kenya's meteorological department and ICPAC, check it against farmers' own rain gauges, and get it to farmers' phones, as the Chicago team did in India. To do that we need partners on the ground and long-term access to the models and station data.

---

## If asked

- **"How good is the AI model, Cumulus?"** "On the six seasons we can test, it's the strongest single model, and it carries most of the blend's weight. Six seasons is still a small sample, so we'll keep testing as the record grows."
- **"Why only two weeks ahead?"** "The official onset definition needs three weeks of weather after the first rains to rule out a false start, so a month-long forecast can only judge the first two weeks. For other events, like a wet week, the blend goes further out."
- **"Why not just use the best single model?"** "The best model changes by lead time and by place. The blend learns that from data instead of us guessing."
- **"Is 30% better a lot?"** "For predicting the week the rains start, yes. Any given week rarely sees the onset, so beating the historical rate at all is hard. It means farmers get usable odds, not a coin flip."
- **"What does it cost to run?"** "A few minutes on a laptop once 2 GB of data is downloaded. The expensive part, running the forecast models themselves, is already done by the weather centres and by Cumulus."
