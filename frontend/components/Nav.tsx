"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Explorer" },
  { href: "/benchmarks", label: "Benchmarks" },
];

export default function Nav() {
  const path = usePathname();
  return (
    <nav className="nav small">
      {LINKS.map((l) => (
        <Link key={l.href} href={l.href} className={path === l.href ? "active" : ""}>
          {l.label}
        </Link>
      ))}
    </nav>
  );
}
