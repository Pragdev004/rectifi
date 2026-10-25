// A small markdown reader for the assistant's replies. It builds React elements, never HTML strings, so nothing the model
// writes can become markup: `<script>` is shown as text, and a link is only made for http(s) addresses.
import { Fragment, type ReactNode } from "react";

export type Inline =
  | { t: "text"; v: string }
  | { t: "strong"; c: Inline[] }
  | { t: "em"; c: Inline[] }
  | { t: "code"; v: string }
  | { t: "link"; href: string; c: Inline[] }
  | { t: "br" };

export type Block =
  | { t: "p"; c: Inline[] }
  | { t: "h"; level: 3 | 4 | 5; c: Inline[] }
  | { t: "ul" | "ol"; items: Inline[][] }
  | { t: "quote"; c: Inline[] }
  | { t: "pre"; v: string }
  | { t: "table"; head: Inline[][]; rows: Inline[][][] }
  | { t: "hr" };

const SAFE_LINK = /^https?:\/\/[^\s<>"']+$/i;

export function parseInline(src: string): Inline[] {
  const out: Inline[] = [];
  let text = "";
  const flush = () => {
    if (text) out.push({ t: "text", v: text });
    text = "";
  };
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (ch === "`") {
      const end = src.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        out.push({ t: "code", v: src.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    if ((ch === "*" && src[i + 1] === "*") || (ch === "_" && src[i + 1] === "_")) {
      const mark = ch + ch;
      const end = src.indexOf(mark, i + 2);
      if (end > i + 2) {
        flush();
        out.push({ t: "strong", c: parseInline(src.slice(i + 2, end)) });
        i = end + 2;
        continue;
      }
    }
    if (ch === "*" && src[i + 1] !== " " && src[i + 1] !== "*") {
      const end = src.indexOf("*", i + 1);
      if (end > i + 1 && src[end - 1] !== " ") {
        flush();
        out.push({ t: "em", c: parseInline(src.slice(i + 1, end)) });
        i = end + 1;
        continue;
      }
    }
    if (ch === "[") {
      const close = src.indexOf("](", i + 1);
      const end = close > 0 ? src.indexOf(")", close + 2) : -1;
      if (close > i && end > close) {
        const label = src.slice(i + 1, close);
        const href = src.slice(close + 2, end).trim();
        if (SAFE_LINK.test(href)) {
          flush();
          out.push({ t: "link", href, c: parseInline(label) });
          i = end + 1;
          continue;
        }
      }
    }
    if (ch === "\n") {
      flush();
      out.push({ t: "br" });
      i += 1;
      continue;
    }
    text += ch;
    i += 1;
  }
  flush();
  return out;
}

const isBullet = (l: string) => /^\s{0,3}[-*+]\s+\S/.test(l);
const isNumbered = (l: string) => /^\s{0,3}\d{1,3}[.)]\s+\S/.test(l);
const isRow = (l: string) => /^\s*\|.*\|\s*$/.test(l);
const isSeparator = (l: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l) && l.includes("-");
const cells = (l: string) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

export function parseBlocks(src: string): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  const L = (n: number) => lines[n] ?? "";
  let i = 0;
  while (i < lines.length) {
    const line = L(i);
    if (line.trim() === "") { i += 1; continue; }
    if (/^\s*```/.test(line)) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(L(i))) { body.push(L(i)); i += 1; }
      i += 1;
      blocks.push({ t: "pre", v: body.join("\n") });
      continue;
    }
    const heading = /^(#{1,6})\s+(.*\S)\s*$/.exec(line);
    if (heading) {
      const level = Math.min(5, Math.max(3, (heading[1] ?? "").length + 2)) as 3 | 4 | 5;
      blocks.push({ t: "h", level, c: parseInline(heading[2] ?? "") });
      i += 1;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { blocks.push({ t: "hr" }); i += 1; continue; }
    if (isRow(line) && i + 1 < lines.length && isSeparator(L(i + 1))) {
      const head = cells(line).map(parseInline);
      const rows: Inline[][][] = [];
      i += 2;
      while (i < lines.length && isRow(L(i))) { rows.push(cells(L(i)).map(parseInline)); i += 1; }
      blocks.push({ t: "table", head, rows });
      continue;
    }
    if (isBullet(line) || isNumbered(line)) {
      const ordered = isNumbered(line);
      const items: Inline[][] = [];
      while (i < lines.length && (ordered ? isNumbered(L(i)) : isBullet(L(i)))) {
        items.push(parseInline(L(i).replace(ordered ? /^\s{0,3}\d{1,3}[.)]\s+/ : /^\s{0,3}[-*+]\s+/, "")));
        i += 1;
      }
      blocks.push({ t: ordered ? "ol" : "ul", items });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(L(i))) { body.push(L(i).replace(/^\s*>\s?/, "")); i += 1; }
      blocks.push({ t: "quote", c: parseInline(body.join("\n")) });
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length && L(i).trim() !== "" && !/^\s*```/.test(L(i)) && !/^#{1,6}\s/.test(L(i)) &&
      !isBullet(L(i)) && !isNumbered(L(i)) && !/^\s*>/.test(L(i))
    ) { para.push(L(i)); i += 1; }
    blocks.push({ t: "p", c: parseInline(para.join("\n")) });
  }
  return blocks;
}

function renderInline(nodes: Inline[]): ReactNode {
  return nodes.map((n, i) => {
    switch (n.t) {
      case "text": return <Fragment key={i}>{n.v}</Fragment>;
      case "strong": return <strong key={i}>{renderInline(n.c)}</strong>;
      case "em": return <em key={i}>{renderInline(n.c)}</em>;
      case "code": return <code key={i}>{n.v}</code>;
      case "br": return <br key={i} />;
      case "link": return <a key={i} href={n.href} target="_blank" rel="noopener noreferrer">{renderInline(n.c)}</a>;
    }
  });
}

export function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={className}>
      {parseBlocks(text).map((b, i) => {
        switch (b.t) {
          case "p": return <p key={i}>{renderInline(b.c)}</p>;
          case "h": { const H = (`h${b.level}`) as "h3" | "h4" | "h5"; return <H key={i}>{renderInline(b.c)}</H>; }
          case "ul": return <ul key={i}>{b.items.map((c, j) => <li key={j}>{renderInline(c)}</li>)}</ul>;
          case "ol": return <ol key={i}>{b.items.map((c, j) => <li key={j}>{renderInline(c)}</li>)}</ol>;
          case "quote": return <blockquote key={i}>{renderInline(b.c)}</blockquote>;
          case "pre": return <pre key={i}><code>{b.v}</code></pre>;
          case "hr": return <hr key={i} />;
          case "table":
            return (
              <div key={i} className="md-table">
                <table>
                  <thead><tr>{b.head.map((c, j) => <th key={j} scope="col">{renderInline(c)}</th>)}</tr></thead>
                  <tbody>{b.rows.map((r, j) => <tr key={j}>{r.map((c, k) => <td key={k}>{renderInline(c)}</td>)}</tr>)}</tbody>
                </table>
              </div>
            );
        }
      })}
    </div>
  );
}
