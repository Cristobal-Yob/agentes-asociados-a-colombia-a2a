import unicodedata

from common.contracts import Source

CATALOG = {
    "bogota": (
        "Bogota",
        "https://monserrate.co/en/plan-your-visit/",
        [
            ("bogota-gold", "Gold Museum", "culture"),
            ("bogota-candelaria", "La Candelaria walk", "culture"),
            ("bogota-monserrate", "Monserrate viewpoint", "nature"),
        ],
    ),
    "medellin": (
        "Medellin",
        "https://colombia.travel/en/medellin?page=0",
        [
            ("medellin-botero", "Plaza Botero", "culture"),
            ("medellin-arvi", "Parque Arvi nature walk", "nature"),
            ("medellin-comuna", "Comuna 13 guided walk", "culture"),
        ],
    ),
    "cartagena": (
        "Cartagena",
        "https://colombia.travel/en/cartagena/colonial-architecture-cartagena",
        [
            ("cartagena-walls", "Historic walled city walk", "culture"),
            ("cartagena-clock", "Clock Tower and Portal de los Dulces walk", "culture"),
            ("cartagena-castle", "San Felipe castle", "culture"),
        ],
    ),
}


def normalize(value):
    return "".join(
        c for c in unicodedata.normalize("NFD", value.lower()) if not unicodedata.combining(c)
    ).strip()


class Catalog:
    def __init__(self, mcp=None):
        self.mcp = mcp
        self.calls = 0

    async def retrieve(self, city, interests):
        self.calls += 1
        key = normalize(city)
        if key not in CATALOG:
            return [], "unsupported_city"
        name, url, entries = CATALOG[key]
        sources = [
            Source(
                id=id_,
                name=label,
                city=name,
                url=url,
                retrieved_at=None,
                fixture_as_of="2026-10-06",
                source_reviewed_at="2026-10-06",
                provenance="curated_fixture",
            )
            for id_, label, _ in entries
        ]
        preferred = {id_ for id_, _, tag in entries if tag in interests}
        sources.sort(key=lambda source: source.id not in preferred)
        status = "fixture_only"
        if self.mcp:
            live, status = await self.mcp.retrieve(name)
            sources.extend(live)
        return sources[:8], status
