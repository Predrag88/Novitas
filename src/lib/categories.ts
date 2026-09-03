export const CATEGORIES = [
  { name: 'Radionice', slug: 'radionice' },
  { name: 'Medijski prostor', slug: 'medijski-prostor' },
  { name: 'Vesti', slug: 'vesti' },
  { name: 'Dokumenti', slug: 'dokumenti' },
] as const;

export type CategoryName = (typeof CATEGORIES)[number]['name'];

export const CATEGORY_NAMES = CATEGORIES.map((c) => c.name) as [CategoryName, ...CategoryName[]];

export function categorySlug(name: string): string {
  return CATEGORIES.find((c) => c.name === name)?.slug ?? name.toLowerCase();
}

export function categoryByName(name: string) {
  return CATEGORIES.find((c) => c.name === name);
}

export function categoryBySlug(slug: string) {
  return CATEGORIES.find((c) => c.slug === slug);
}
