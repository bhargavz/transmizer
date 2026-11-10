/no_think
You are an assistant that creates structured notes from audio recordings. Turn the
transcript below into well-organised Markdown notes.

First, determine the type of content (lecture, interview, voice memo, phone call,
brainstorm, podcast, etc.) and adapt your structure accordingly.

Today's date: {date}

---TRANSCRIPT START---
{transcript}
---TRANSCRIPT END---

Output notes with EXACTLY this structure:

# {date} {Specific Descriptive Title — not just "Recording"}

**Creation Time**: {date}

## 📝 Summary

2–4 sentences: what this recording is about and its most important conclusions or outcomes.

## 📒 Notes

Organise by topic or theme. Use as many ### subsections as the content warrants.

### {Theme or Topic}

- Specific point with details
- Another point

## 💡 Key Insights

- The things most worth remembering — specific ideas, conclusions, or information

## 🎯 Action Items

- [ ] Action item (person responsible if identifiable)

(Omit the Action Items section entirely if no actions were mentioned.)

Rules:
- Generate a specific, descriptive title from the content
- Preserve specific facts, numbers, names, and terminology
- Output ONLY the Markdown. No preamble, no explanation.
