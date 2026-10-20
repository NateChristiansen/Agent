// The Dashboard's nav line: today's journal note, the open feature count, and the home links
// Computed when the note renders, so it never points at yesterday the way the
// written line did. The path format is `src/agent_cli/journal/paths.py:note_rel`.

// English month and weekday names whatever Obsidian's locale, as the CLI writes them
const today = dv.luxon.DateTime.now().setLocale("en");
const rel = `journal/${today.toFormat("yyyy")}/${today.toFormat("MM")}-${today.toFormat("MMMM")}/${today.toFormat("dd")}-${today.toFormat("cccc")}`;
const hasNote = !!app.vault.getAbstractFileByPath(`${rel}.md`);
const open = dv.pages('"features"').where((p) => p.file.name === "plan" && ["proposed", "active", "blocked"].includes(p.status)).length;

const todayLink = hasNote ? `[[${rel}|today]]` : "[today](obsidian://daily)";
dv.paragraph(`[[Dashboard|dashboard]] · ${todayLink} · [[Index|index]] · [[AGENTS#Standing rules|rules]] · ${open} open features`);
