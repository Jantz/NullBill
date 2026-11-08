# Accessibility

NullBill's only graphical interface is the setup and status page
(`src/nullbill/web/static/`). It is plain HTML with native form controls: a
skip link, one `main`, a labelled navigation whose current page carries
`aria-current`, and an `h1` per page that takes focus when the page
changes. Choices are real checkboxes and radios inside their labels,
grouped in fieldsets; the agent's root switch is a `role=switch` button.
Buttons that must wait keep focus (`aria-disabled`) and say why beside
themselves. A running job announces each new stage and its end through
one polite live region, not every line of output. Status colours always
come with words, and animation slows under reduced motion.

It has been checked in Chromium at phone and desktop widths, in light
and dark mode, with scripted keyboard and focus checks, but not yet with
a screen reader. Known limits: the setup log is long preformatted text, and the
on-site screen shows the page in a kiosk browser without its own
accessibility settings.

If something on the page stops you from using it, open an issue and say
which browser and assistive technology you use.
