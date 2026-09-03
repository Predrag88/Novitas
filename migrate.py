#!/usr/bin/env python3
"""
Jednokratna migraciona skripta: WordPress backup (tar.gz) -> Astro content collection.

BEZBEDNOST:
- Arhiva sadrzi malver. Iz nje se NIKAD ne cita/pise/pokrece nijedan .php, .js, .ico ili
  drugi izvrsni fajl.
- Nikad se ne koristi tar.extractall() - iskljucivo pojedinacno citanje clanova preko
  tarfile.extractfile() za: SQL dump i slike (dozvoljene ekstenzije) iz wp-content/uploads/.

Upotreba:
    .venv/bin/python migrate.py --report        # samo analiza, nista se ne pise na disk
    .venv/bin/python migrate.py --write          # generise .md fajlove + kopira slike
"""
import argparse
import datetime
import re
import sys
import tarfile
from pathlib import Path

ARCHIVE = Path("backup-vidljiva.rs-01.15.2026_12-21-06.tar.gz")
SQL_MEMBER = "./OMfRXS2Amt89Je.sql"
UPLOADS_PREFIX = "public_html/public_html/wp-content/uploads/"
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".svg", ".gif"}

CONTENT_DIR = Path("src/content/blog")
PAGES_CONTENT_DIR = Path("src/content/pages")
IMAGES_DIR = Path("public/images/posts")

WP_POSTS_COLUMNS = [
    "ID", "post_author", "post_date", "post_date_gmt", "post_content", "post_title",
    "post_excerpt", "post_status", "comment_status", "ping_status", "post_password",
    "post_name", "to_ping", "pinged", "post_modified", "post_modified_gmt",
    "post_content_filtered", "post_parent", "guid", "menu_order", "post_type",
    "post_mime_type", "comment_count",
]
WP_POSTMETA_COLUMNS = ["meta_id", "post_id", "meta_key", "meta_value"]
WP_TERMS_COLUMNS = ["term_id", "name", "slug", "term_group"]
WP_TERM_TAXONOMY_COLUMNS = ["term_taxonomy_id", "term_id", "taxonomy", "description", "parent", "count"]
WP_TERM_RELATIONSHIPS_COLUMNS = ["object_id", "term_taxonomy_id", "term_order"]

# WP kategorija (izvorno ime u bazi, uklj. tipfeler "Medjiski prostor") -> ispravljen naziv/slug
# koji koristimo na sajtu. Ove 4 kategorije u originalnom WP-u odgovaraju posebnim
# stranicama (Medijski prostor, Vesti, Dokumenti, Radionice/Blog) koje su svaka samo
# [blog_posts cat="X"] filter - na Astro sajtu to postaju /kategorija/<slug>/ stranice.
CATEGORY_NORMALIZE = {
    "Radionice": ("Radionice", "radionice"),
    "Medjiski prostor": ("Medijski prostor", "medijski-prostor"),
    "Vesti": ("Vesti", "vesti"),
    "Dokumenti": ("Dokumenti", "dokumenti"),
}

# Prave WP stranice (post_type='page') koje rekonstruisemo kao Astro stranice.
# 'Blog'(22)/'Medijski prostor'(2355)/'Vesti'(2390)/'Dokumenti'(2445) NISU ovde jer
# su to u WP-u bile cisto [blog_posts cat=X] filter-stranice - te postaju
# /kategorija/<slug>/ stranice generisane iz kategorije posta, ne staticki sadrzaj.
STATIC_PAGE_IDS = ["2191", "1731", "2456", "2522", "2233"]


# ---------------------------------------------------------------------------
# Minimalni SQL-values tokenizer (bez spoljnih SQL-parsing biblioteka)
# ---------------------------------------------------------------------------

def split_sql_tuple(line: str):
    """Parsira jedan '(...);' ili '(...),' red mysqldump-a u listu Python vrednosti.

    Rucni state-machine jer post_content sadrzi zapete, navodnike i escape sekvence
    (\\', \\\\, \\n, \\r, \\") - regex/CSV split nije bezbedan za to.
    """
    line = line.strip()
    if line.startswith("("):
        line = line[1:]
    # ukloni zavrsni ');' ili '),' ili ')'
    line = re.sub(r"\)\s*[;,]?\s*$", "", line)

    values = []
    buf = []
    i = 0
    n = len(line)
    in_string = False
    while i < n:
        ch = line[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                nxt = line[i + 1]
                mapping = {"n": "\n", "r": "\r", "t": "\t", "'": "'", '"': '"', "\\": "\\", "0": "\0"}
                buf.append(mapping.get(nxt, nxt))
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and line[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_string = False
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
        else:
            if ch == "'":
                in_string = True
                i += 1
                continue
            if ch == ",":
                values.append("".join(buf))
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
            continue
    values.append("".join(buf))

    cleaned = []
    for v in values:
        v_stripped = v.strip()
        if v_stripped == "NULL":
            cleaned.append(None)
        else:
            cleaned.append(v)
    return cleaned


def iter_insert_rows(sql_text: str, table_name: str):
    """Generator koji vraca listu vrednosti (kolona) za svaki red date tabele."""
    marker = f"INSERT INTO `{table_name}` VALUES"
    lines = sql_text.split("\n")
    i = 0
    in_block = False
    while i < len(lines):
        line = lines[i]
        if line.startswith(marker):
            in_block = True
            i += 1
            continue
        if in_block:
            if line.startswith("(") :
                yield split_sql_tuple(line)
                i += 1
                continue
            else:
                in_block = False
                continue
        i += 1


# ---------------------------------------------------------------------------
# Ekstrakcija iz arhive (samo SQL i slike, nikad ceo tar)
# ---------------------------------------------------------------------------

def read_sql_dump() -> str:
    with tarfile.open(ARCHIVE, "r:gz") as tar:
        member = tar.getmember(SQL_MEMBER)
        f = tar.extractfile(member)
        if f is None:
            raise RuntimeError(f"Ne mogu da procitam {SQL_MEMBER} iz arhive")
        return f.read().decode("utf-8", errors="replace")


def list_valid_image_members():
    """Vraca listu (relative_uploads_path, tarfile.TarInfo) za dozvoljene slike."""
    result = []
    with tarfile.open(ARCHIVE, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name[2:] if member.name.startswith("./") else member.name
            if not name.startswith(UPLOADS_PREFIX):
                continue
            ext = Path(name).suffix.lower()
            if ext not in ALLOWED_IMAGE_EXT:
                continue
            rel = name[len(UPLOADS_PREFIX):]
            result.append((rel, member))
    return result


def extract_image(member, dest_path: Path):
    with tarfile.open(ARCHIVE, "r:gz") as tar:
        f = tar.extractfile(member)
        if f is None:
            return False
        dest_path.write_bytes(f.read())
        return True


# ---------------------------------------------------------------------------
# Sadrzaj: ciscenje HTML/Gutenberg -> Markdown
# ---------------------------------------------------------------------------

GUTENBERG_COMMENT_RE = re.compile(r"<!--\s*/?wp:[^>]*-->")
SHORTCODE_RE = re.compile(r"\[/?[a-zA-Z][^\]\[]*\]")
UPLOADS_URL_RE = re.compile(r"(?:https?://[^/\"'\s]+)?/?wp-content/uploads/[0-9]{4}/[0-9]{2}/([^\"'\s)]+)")
UX_VIDEO_RE = re.compile(r'\[ux_video\s+url="([^"]+)"[^\]]*\]')
BG_ID_RE = re.compile(r'bg="(\d+)"')


def _video_shortcode_to_placeholder(html: str) -> str:
    """[ux_video url="https://youtube..."] -> pravi <a> tag (ne obican tekst!), da bi ga
    markdownify pretvorio u [Pogledaj video](url) BEZ escape-ovanja '_' unutar URL-a
    (markdownify escape-uje markdown specijalne karaktere samo u tekstualnim cvorovima,
    ne u href atributima). Nikad ne generisemo <iframe> u izlazu (bezbednosno pravilo),
    pa i legitimne YouTube embed-ove pretvaramo u obicne linkove.
    """
    return UX_VIDEO_RE.sub(lambda m: f'\n\n<a href="{m.group(1)}">Pogledaj video</a>\n\n', html)


def clean_html_to_markdown(html: str) -> str:
    from bs4 import BeautifulSoup
    from markdownify import markdownify as md

    html = _video_shortcode_to_placeholder(html)
    html = GUTENBERG_COMMENT_RE.sub("", html)
    html = SHORTCODE_RE.sub("", html)

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all("iframe"):
        src = tag.get("src", "")
        if src.startswith("https://") or src.startswith("http://"):
            link = soup.new_tag("a", href=src)
            link.string = "Otvori"
            tag.replace_with(link)
        else:
            tag.decompose()

    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    for tag in soup.find_all(True):
        if tag.has_attr("style"):
            del tag["style"]
        for attr in list(tag.attrs):
            if attr.startswith("on"):  # onclick, onerror, itd.
                del tag[attr]

    for tag in soup.find_all("img"):
        if not tag.get("src"):
            # WP page-builder "spacer"/zero-width markup bez src-a - nema stvarni sadrzaj
            tag.decompose()

    for tag in soup.find_all(["img"]):
        src = tag.get("src", "")
        new_src = UPLOADS_URL_RE.sub(lambda m: "/images/posts/" + Path(m.group(1)).name, src)
        if new_src != src:
            tag["src"] = new_src

    for tag in soup.find_all("a"):
        href = tag.get("href", "")
        new_href = UPLOADS_URL_RE.sub(lambda m: "/images/posts/" + Path(m.group(1)).name, href)
        if new_href != href:
            tag["href"] = new_href

    cleaned_html = str(soup)
    markdown = md(cleaned_html, heading_style="ATX")
    # rewrite ostataka uploads url-ova u samom markdown izlazu (npr. u markdown link sintaksi)
    markdown = UPLOADS_URL_RE.sub(lambda m: "/images/posts/" + Path(m.group(1)).name, markdown)
    # ukloni visak praznih linija
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"
    return markdown


def strip_tags_for_excerpt(html: str) -> str:
    from bs4 import BeautifulSoup
    html = GUTENBERG_COMMENT_RE.sub("", html)
    html = SHORTCODE_RE.sub("", html)
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def slugify_fallback(title: str) -> str:
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "post"


def yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Glavna logika
# ---------------------------------------------------------------------------

def _load_wp_tables(sql_text):
    posts = [dict(zip(WP_POSTS_COLUMNS, r)) for r in iter_insert_rows(sql_text, "wp_posts")
             if len(r) == len(WP_POSTS_COLUMNS)]
    postmeta = [dict(zip(WP_POSTMETA_COLUMNS, r)) for r in iter_insert_rows(sql_text, "wp_postmeta")
                if len(r) == len(WP_POSTMETA_COLUMNS)]
    terms = [dict(zip(WP_TERMS_COLUMNS, r)) for r in iter_insert_rows(sql_text, "wp_terms")
             if len(r) == len(WP_TERMS_COLUMNS)]
    term_taxonomy = [dict(zip(WP_TERM_TAXONOMY_COLUMNS, r)) for r in iter_insert_rows(sql_text, "wp_term_taxonomy")
                      if len(r) == len(WP_TERM_TAXONOMY_COLUMNS)]
    term_relationships = [dict(zip(WP_TERM_RELATIONSHIPS_COLUMNS, r))
                           for r in iter_insert_rows(sql_text, "wp_term_relationships")
                           if len(r) == len(WP_TERM_RELATIONSHIPS_COLUMNS)]
    return posts, postmeta, terms, term_taxonomy, term_relationships


def _build_category_map(terms, term_taxonomy, term_relationships):
    """post_id -> (naziv, slug) za primarnu WP kategoriju tog posta (preskace 'Uncategorized')."""
    term_by_id = {t["term_id"]: t for t in terms}
    cat_name_by_tt_id = {}
    for tt in term_taxonomy:
        if tt["taxonomy"] == "category":
            term = term_by_id.get(tt["term_id"])
            if term:
                cat_name_by_tt_id[tt["term_taxonomy_id"]] = term["name"]

    category_of = {}
    for rel in term_relationships:
        raw_name = cat_name_by_tt_id.get(rel["term_taxonomy_id"])
        if raw_name is None:
            continue
        normalized = CATEGORY_NORMALIZE.get(raw_name)
        if normalized is None:
            continue  # npr. 'Uncategorized' - nema odgovarajucu stranicu, preskacemo
        if rel["object_id"] not in category_of:
            category_of[rel["object_id"]] = normalized
    return category_of


def build_posts():
    sql_text = read_sql_dump()
    posts, postmeta, terms, term_taxonomy, term_relationships = _load_wp_tables(sql_text)

    thumbnail_of = {}
    attached_file = {}
    for meta in postmeta:
        key = meta["meta_key"]
        if key == "_thumbnail_id":
            thumbnail_of[meta["post_id"]] = meta["meta_value"]
        elif key == "_wp_attached_file":
            attached_file[meta["post_id"]] = meta["meta_value"]

    category_of = _build_category_map(terms, term_taxonomy, term_relationships)

    published = [p for p in posts if p["post_type"] == "post" and p["post_status"] == "publish"]
    published.sort(key=lambda p: p["post_date"])

    enriched = []
    for p in published:
        thumb_id = thumbnail_of.get(p["ID"])
        rel_path = attached_file.get(thumb_id) if thumb_id else None
        hero = f"/images/posts/{Path(rel_path).name}" if rel_path else None
        cat_name, cat_slug = category_of.get(p["ID"], (None, None))
        enriched.append({
            "id": p["ID"],
            "title": p["post_title"],
            "slug": p["post_name"] or slugify_fallback(p["post_title"]),
            "date": p["post_date"],
            "content": p["post_content"],
            "excerpt": p["post_excerpt"],
            "hero_rel_path": rel_path,
            "hero_image": hero,
            "category": cat_name,
            "category_slug": cat_slug,
        })
    return enriched


def build_pages():
    sql_text = read_sql_dump()
    posts, postmeta, terms, term_taxonomy, term_relationships = _load_wp_tables(sql_text)
    by_id = {p["ID"]: p for p in posts}
    attached_file = {m["post_id"]: m["meta_value"] for m in postmeta if m["meta_key"] == "_wp_attached_file"}

    pages = []
    for pid in STATIC_PAGE_IDS:
        p = by_id.get(pid)
        if p is None or p["post_status"] != "publish":
            continue
        bg_ids = BG_ID_RE.findall(p["post_content"])
        hero_rel_path = None
        for bg_id in bg_ids:
            if bg_id in attached_file:
                hero_rel_path = attached_file[bg_id]
                break
        hero = f"/images/posts/{Path(hero_rel_path).name}" if hero_rel_path else None
        pages.append({
            "id": p["ID"],
            "title": p["post_title"],
            "slug": p["post_name"],
            "content": p["post_content"],
            "excerpt": p["post_excerpt"],
            "hero_rel_path": hero_rel_path,
            "hero_image": hero,
        })
    return pages


def report(posts, pages):
    print(f"\nPronadjeno {len(posts)} objavljenih postova (post_type='post', post_status='publish'):\n")
    for p in posts:
        hero_note = "sa hero slikom" if p["hero_image"] else "BEZ hero slike"
        cat_note = p["category"] or "bez kategorije"
        print(f"  - [{p['date'][:10]}] {p['title']}  (slug: {p['slug']}, kategorija: {cat_note}, {hero_note})")
    print(f"\nPronadjeno {len(pages)} statickih stranica:\n")
    for p in pages:
        hero_note = "sa hero slikom" if p["hero_image"] else "bez hero slike"
        print(f"  - {p['title']}  (slug: {p['slug']}, {hero_note})")
    print()


# Slike koje homepage koristi direktno (hero banner, about sekcija, itd.) - nisu
# referencirane ni u jednom generisanom .md fajlu, pa ih eksplicitno trazimo po imenu.
HOMEPAGE_IMAGES = {
    "vidljivahomebanner003.jpg",  # Hero banner
    "vidljivahomebanner3.jpg",    # About sekcija
    "slika2.jpg",                 # Testimonials/workshop CTA sekcija
}


def write_output(posts, pages):
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    valid_images = list_valid_image_members()
    by_basename = {}
    for rel, member in valid_images:
        by_basename.setdefault(Path(rel).name, []).append((rel, member))

    referenced_basenames = set(HOMEPAGE_IMAGES)
    md_bodies = {}
    for p in posts:
        md_bodies[p["id"]] = clean_html_to_markdown(p["content"])
        for m in re.finditer(r"/images/posts/([^\s\"'()]+)", md_bodies[p["id"]]):
            referenced_basenames.add(m.group(1))
        if p["hero_image"]:
            referenced_basenames.add(Path(p["hero_image"]).name)

    page_md_bodies = {}
    for p in pages:
        page_md_bodies[p["id"]] = clean_html_to_markdown(p["content"])
        for m in re.finditer(r"/images/posts/([^\s\"'()]+)", page_md_bodies[p["id"]]):
            referenced_basenames.add(m.group(1))
        if p["hero_image"]:
            referenced_basenames.add(Path(p["hero_image"]).name)

    copied = 0
    skipped_missing = []
    for basename in sorted(referenced_basenames):
        candidates = by_basename.get(basename)
        if not candidates:
            skipped_missing.append(basename)
            continue
        rel, member = candidates[0]
        dest = IMAGES_DIR / basename
        if not dest.exists():
            extract_image(member, dest)
            copied += 1

    written = 0
    no_hero = []
    for p in posts:
        title = yaml_escape(p["title"])
        excerpt = p["excerpt"].strip()
        if excerpt:
            description = strip_tags_for_excerpt(excerpt)
        else:
            description = strip_tags_for_excerpt(p["content"])[:160].rsplit(" ", 1)[0]
        description = yaml_escape(description)

        pub_date = p["date"][:10]
        try:
            datetime.date.fromisoformat(pub_date)
        except ValueError:
            pub_date = datetime.date.today().isoformat()

        hero_line = f'heroImage: "{p["hero_image"]}"\n' if p["hero_image"] else ""
        if not p["hero_image"]:
            no_hero.append(p["title"])
        category_line = f'category: "{p["category"]}"\n' if p["category"] else ""

        frontmatter = (
            "---\n"
            f'title: "{title}"\n'
            f'description: "{description}"\n'
            f"pubDate: {pub_date}\n"
            f"{hero_line}"
            f"{category_line}"
            "draft: false\n"
            "---\n\n"
        )
        out_path = CONTENT_DIR / f"{p['slug']}.md"
        out_path.write_text(frontmatter + md_bodies[p["id"]], encoding="utf-8")
        written += 1

    pages_written = 0
    for p in pages:
        title = yaml_escape(p["title"])
        excerpt = p["excerpt"].strip()
        if excerpt:
            description = strip_tags_for_excerpt(excerpt)
        else:
            description = strip_tags_for_excerpt(p["content"])[:160].rsplit(" ", 1)[0]
        description = yaml_escape(description)
        hero_line = f'heroImage: "{p["hero_image"]}"\n' if p["hero_image"] else ""

        frontmatter = (
            "---\n"
            f'title: "{title}"\n'
            f'description: "{description}"\n'
            f"{hero_line}"
            "---\n\n"
        )
        out_path = PAGES_CONTENT_DIR / f"{p['slug']}.md"
        out_path.write_text(frontmatter + page_md_bodies[p["id"]], encoding="utf-8")
        pages_written += 1

    print(f"\nGenerisano {written} blog .md fajlova u {CONTENT_DIR}/")
    print(f"Generisano {pages_written} stranica u {PAGES_CONTENT_DIR}/")
    print(f"Kopirano {copied} slika u {IMAGES_DIR}/ (od {len(referenced_basenames)} referenciranih)")
    if skipped_missing:
        print(f"UPOZORENJE: {len(skipped_missing)} referenciranih slika nije pronadjeno u uploads/: {skipped_missing}")
    if no_hero:
        print(f"Napomena: {len(no_hero)} post(ova) bez hero slike: {no_hero}")


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report", action="store_true", help="Samo analiza, ne pise nista na disk")
    group.add_argument("--write", action="store_true", help="Generise .md fajlove i kopira slike")
    args = parser.parse_args()

    if not ARCHIVE.exists():
        print(f"Arhiva {ARCHIVE} nije pronadjena.", file=sys.stderr)
        sys.exit(1)

    posts = build_posts()
    pages = build_pages()

    if args.report:
        report(posts, pages)
    elif args.write:
        write_output(posts, pages)


if __name__ == "__main__":
    main()
