# Huashu Design

Huashu is a bilingual (Arabic/English) design system optimised for product
teams shipping consumer apps and dashboards. This skill turns a high-level
brief into concrete design artefacts:

- **Brand kit** — colour palette, type scale, spacing tokens, motion presets.
- **Layouts** — wireframes anchored to the Huashu modular grid (8/4 px) with
  proper RTL mirroring.
- **Component recipes** — opinionated React + Tailwind / Flutter recipes
  that snap to the brand kit.
- **Audits** — review an existing screen against the Huashu rules and emit
  a numbered diff list.

## When to use

- New product needs a from-scratch identity in under a day.
- An existing UI needs to be Arabicised without breaking the English
  experience.
- Marketing assets need to follow a single, documented system.

## Outputs

| Artefact | Format |
|---|---|
| Brand kit | JSON design tokens + Markdown rationale |
| Wireframe | SVG / Figma URL |
| Component | React/TSX or Flutter Dart snippet |
| Audit | Markdown checklist with severity tags |

## Inputs

```
{
  "brief": "string — what we're designing for and for whom",
  "languages": ["ar", "en"],
  "primary_color": "#... (optional)",
  "tone": "playful|formal|technical (optional)"
}
```

## Notes

- All copy is delivered in both Arabic and English by default.
- Tokens follow the [Style Dictionary](https://styledictionary.com) schema
  so they drop into Tailwind / Flutter pipelines.
- The component recipes assume Tailwind ≥ 3.4 or Flutter ≥ 3.16.
