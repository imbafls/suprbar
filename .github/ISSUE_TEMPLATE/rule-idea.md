---
name: Rule idea
about: Propose a new coach rule (observation the tray should surface)
title: "[rule] "
labels: rule-idea
---

## Rule id

A short stable kebab-case id. Example: `late-night-tax`.

## Title (what the user sees)

One short sentence-case line, no period. Example: "After-hours sessions ship less."

## Trigger

Plain-English condition. Cite the JSONL fields or `SessionContext`
properties you would inspect.

## Body

One to three sentences of the observation body, written in second person.

## Tip (optional)

A single concrete next action.

## Severity

`info` / `nudge` / `warn` — and why.

## Confidence model

How would the rule decide its own `confidence`? A constant? A ratio?
A regression against the user's own history?

## Why it's worth surfacing

What action does the observation make likely that wouldn't happen
otherwise? If you can't answer this, the rule is probably noise.
