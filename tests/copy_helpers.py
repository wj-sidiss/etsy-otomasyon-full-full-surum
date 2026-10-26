"""Invented marketplace samples for the title and tag builders, run through the real research.

A market is a handful of product groups, each with the title heads its listings open with,
the phrases they follow with and the tags they carry; a seeded generator draws 200 listings
from them, so the same call always gives the same sample. Nothing here is a real listing.
"""

from __future__ import annotations

import random

from etsyotomasyon.seo import MarketReport, research


class Search:
    def __init__(self, listings):
        self.listings = listings

    def search_active_listings(self, *, keywords, max_items=100, **_filters):
        return self.listings[:max_items]


def _pick(rng, pool, k):
    """k distinct texts from [(text, weight)], weighted; seeded, so always the same."""
    items, out = list(pool), []
    for _ in range(min(k, len(items))):
        roll = rng.random() * sum(w for _, w in items)
        for i, (_text, weight) in enumerate(items):
            roll -= weight
            if roll <= 0:
                out.append(items.pop(i)[0])
                break
    return out


def market(keyword, groups, n=200) -> MarketReport:
    """groups: [(share, heads, phrases, tags)], phrases and tags as [(text, weight)]."""
    rng = random.Random(keyword)
    listings = []
    for i in range(n):
        roll, acc, group = rng.random(), 0.0, groups[-1]
        for candidate in groups:
            acc += candidate[0]
            if roll <= acc:
                group = candidate
                break
        _share, heads, phrases, tags = group
        title = ", ".join([rng.choice(heads), *_pick(rng, phrases, rng.randint(3, 5))])
        listings.append({"listing_id": 4000000 + i, "title": title, "tags": _pick(rng, tags, 13),
                         "price": {"amount": 2000, "divisor": 100, "currency_code": "USD"},
                         "num_favorers": i})
    return research(Search(listings), keyword, sample=n)


SHIRT_TAGS = [("graphic tee", 9), ("gift for him", 6), ("gift for her", 7),
              ("unisex tshirt", 6), ("comfort colors", 7), ("vintage shirt", 6),
              ("birthday gift", 4), ("retro shirt", 4)]


def jungle_wallpaper() -> MarketReport:
    """What ranks for a jungle nursery wallpaper: mostly the shop-wide material tags."""
    return market("jungle animals wallpaper", [
        (1.0,
         ["Jungle Nursery Wallpaper", "Safari Animals Nursery Wallpaper",
          "Jungle Animal Wall Mural", "Tropical Jungle Peel and Stick Wallpaper"],
         [("Peel and Stick Wallpaper", 9), ("Removable Wallpaper", 8), ("Kids Room Wall Mural", 6),
          ("Baby Boy Nursery Decor", 6), ("Safari Animals Mural", 5), ("Boho Nursery Decor", 5),
          ("Playroom Wall Decor", 4), ("Tropical Leaf Wallpaper", 4),
          ("Self Adhesive Wallpaper", 5), ("Neutral Nursery Wallpaper", 4),
          ("Animal Wallpaper", 4), ("Watercolor Jungle Mural", 3),
          ("Toddler Bedroom Decor", 3), ("Wall Decor", 7)],
         [("peel and stick wallpaper", 12), ("removable wallpaper", 10), ("nursery wallpaper", 9),
          ("jungle wallpaper", 8), ("safari nursery", 7), ("wallpaper mural", 8),
          ("self adhesive wallpaper", 6), ("renter friendly", 6), ("temporary wallpaper", 5),
          ("kids room decor", 5), ("jungle mural", 4), ("animal wallpaper", 4),
          ("safari wallpaper", 4), ("boho nursery", 3), ("baby room decor", 3),
          ("tropical wallpaper", 3), ("playroom wallpaper", 3), ("jungle animals", 3),
          ("nursery decor", 6), ("wall mural", 6)]),
    ])


def cat_shirt() -> MarketReport:
    """A shirt market that also returns mugs."""
    return market("cat mom life shirt", [
        (0.8, ["Cat Mom Shirt", "Cat Lover T-Shirt", "Crazy Cat Lady Tee"],
         [("Cat Lover Gift", 9), ("Cat Mama Shirt", 7), ("Funny Cat Shirt", 7),
          ("Mothers Day Gift", 6), ("Cute Cat Tee", 5), ("Cat Mom Gift", 6),
          ("Gift for Cat Lovers", 5), ("Pet Lover Shirt", 4), ("Kitten Shirt", 3),
          ("Cat Mom Life", 3), ("Unisex Comfort Colors Tee", 5)],
         [("cat mom shirt", 10), ("cat lover gift", 9), ("cat mama", 7), ("funny cat shirt", 7),
          ("mothers day gift", 6), ("pet lover gift", 5), ("cat lady shirt", 5),
          ("crazy cat lady", 4), ("cat mom gift", 6), ("kitten shirt", 3), *SHIRT_TAGS]),
        (0.2, ["Cat Mom Mug", "Cat Lover Coffee Mug"],
         [("Cat Lover Mug", 5), ("Funny Cat Mug", 4)],
         [("cat mug", 8), ("coffee mug", 6), ("cat lover gift", 6)]),
    ])


def paw_shirt(keyword: str) -> MarketReport:
    """A thin shirt market, for a design whose name is not English."""
    return market(keyword, [
        (1.0, ["Cat Paw Print Shirt"], [("Cat Lover Gift", 5), ("Paw Print Tee", 3)],
         [("cat paw shirt", 5), ("cat lover gift", 4), ("paw print", 3), *SHIRT_TAGS]),
    ], n=40)


# What a peel-and-stick wallpaper shop's template listing looks like (invented).
WALLPAPER_TITLE = ("Kitchen Wallpaper | Citrus Grove Lemon Botanical | Peel and Stick | Removable "
                   "| Self Adhesive | Wall Mural")
WALLPAPER_TAGS = ["self adhesive wall", "removable wallpaper", "peel and stick", "self adhesive",
                  "temporary wallpaper", "renter friendly", "wallpaper mural", "cottage kitchen",
                  "country kitchen", "citrus decor", "lemon mural", "coastal wallpaper"]
WALLPAPER_PEEL_ONLY = "Removable and renter friendly. Peel off the backing and stick it to the wall."
WALLPAPER_BOTH = ("Choose Peel and Stick (removable) or Traditional: paste the wall with "
                  "wallpaper paste, which is not included.")
SHIRT_TITLE = "Retro Mountain Sunset Shirt, Vintage Hiking Tee, Hiking Gift"
SHIRT_TEMPLATE_TAGS = ["graphic tee", "unisex tshirt", "comfort colors", "gift for her",
                       "retro mountain sun", "oversized tee"]


def wallpaper_market(motif: str) -> MarketReport:
    """A wallpaper market for one motif ("fig", "gothic moth"): its own phrases, and the
    shop-wide material tags every wallpaper listing carries."""
    name = motif.title()
    return market(f"{motif} wallpaper", [
        (1.0,
         [f"{name} Wallpaper", f"{name} Wall Mural", f"{name} Peel and Stick Wallpaper"],
         [("Peel and Stick Wallpaper", 9), ("Removable Wallpaper", 8), (f"{name} Kitchen Decor", 5),
          (f"{name} Bedroom Wallpaper", 4), ("Boho Accent Wall", 5), ("Farmhouse Kitchen", 4),
          (f"{name} Pattern Mural", 4), ("Cottagecore Bedroom Decor", 3),
          ("Self Adhesive Wallpaper", 5), ("Nursery Wallpaper", 4), ("Wall Decor", 7),
          ("Botanical Garden Wallpaper", 3), ("Modern Living Room Decor", 3)],
         [("peel and stick wallpaper", 12), ("removable wallpaper", 10), ("wallpaper mural", 8),
          (f"{motif} wallpaper", 9), (f"{motif} mural", 6), (f"{motif} decor", 5),
          (f"{motif} kitchen", 4), (f"{motif} pattern", 4), ("self adhesive wallpaper", 6),
          ("renter friendly", 6), ("temporary wallpaper", 5), ("boho decor", 4),
          ("accent wall", 5), ("farmhouse kitchen", 4), ("botanical wallpaper", 4),
          ("cottagecore decor", 3), ("nursery wallpaper", 5)]),
    ])
