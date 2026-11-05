# Accessibility

NullBill's only graphical interface is the setup and status page
(`src/nullbill/web/static/`). It is a single page of plain HTML with
native form controls, headings for each step and no images that carry
information, so it should work with the keyboard, browser zoom and
screen readers.

It has not been tested with assistive technology yet. Known limits: the
setup log is long preformatted text, and the on-site screen shows the page
in a kiosk browser without its own accessibility settings.

If something on the page stops you from using it, open an issue and say
which browser and assistive technology you use.
