"""The drop pipeline: a folder of designs in, a validated listing CSV out.

The whole point of this package is that it stops at a CSV. It composites images,
works out a concept for each one, researches that concept against listings Etsy
actually returns, assembles a title and tags inside Etsy's hard limits, and writes
a row in the exact shape ``etsyotomasyon listings push`` already consumes.

It never talks to Etsy's write endpoints itself. Everything that creates a draft
goes through ``listings.push()``, which is already tested. That boundary is
deliberate: every new line here sits *before* the tested code, not inside it, and a
seller who prefers a spreadsheet can edit the CSV and get an identical result.

It is also structurally incapable of publishing. It never emits a row with a filled
``listing_id``, and Etsy only accepts ``state`` on an update — so nothing this
package produces can flip a listing live.
"""

from __future__ import annotations

__all__ = ["workspace", "seeds", "mockup", "template", "generate", "pipeline"]
