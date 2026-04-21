---
title: Documents
layout: default
nav_order: 5
has_children: true
permalink: /documents/
description: "Deep-dive technical documentation for Whilly Orchestrator — internals, phase flows, and design decisions."
---

# Documents

Deep-dive technical documentation that goes beyond the reference pages. Where the usage reference tells you *what* flags to pass, this section explains *why* things work the way they do and *what* happens inside Whilly at each step.

These pages are written for contributors, integrators, and operators who need to understand the internals — not just the surface API.

---

## Pages in this section

| Page | What it covers |
|---|---|
| [Task Execution Phases]({{ site.baseurl }}/documents/task-execution-phases) | The 8 internal phases Whilly walks through from `--from-issue` to exit code `0` — workspace isolation, agent dispatch, retry logic, and live board sync |
