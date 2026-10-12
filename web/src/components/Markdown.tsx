import { Fragment, type ReactNode } from "react";

/**
 * Minimal, safe Markdown: fenced code, headings, lists, paragraphs, `code` and **bold**.
 * Builds React elements (never innerHTML), and tolerates an unterminated fence while streaming.
 */
export function Markdown({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  const fence = /```([\w+-]*)\n?([\s\S]*?)(?:```|$)/g;
  let last = 0;
  for (const match of text.matchAll(fence)) {
    const start = match.index ?? 0;
    if (start > last) parts.push(<Blocks key={`t${last}`} text={text.slice(last, start)} />);
    parts.push(
      <pre key={`c${start}`} className="code">
        {match[1] && <span className="code-lang">{match[1]}</span>}
        <code>{match[2]?.replace(/\n$/, "")}</code>
      </pre>,
    );
    last = start + match[0].length;
  }
  if (last < text.length) parts.push(<Blocks key={`t${last}`} text={text.slice(last)} />);
  return <div className="markdown">{parts}</div>;
}

function Blocks({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) out.push(<p key={`p${out.length}`}>{inline(para.join(" "))}</p>);
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const items = list.items.map((it, i) => <li key={i}>{inline(it)}</li>);
    out.push(list.ordered ? <ol key={`l${out.length}`}>{items}</ol> : <ul key={`l${out.length}`}>{items}</ul>);
    list = null;
  };
  for (const raw of text.split("\n")) {
    const line = raw.trimEnd();
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (heading) {
      flushPara();
      flushList();
      const level = Math.min(heading[1]!.length + 2, 6);
      const Tag = `h${level}` as "h3" | "h4" | "h5" | "h6";
      out.push(<Tag key={`h${out.length}`}>{inline(heading[2]!)}</Tag>);
    } else if (bullet || numbered) {
      flushPara();
      const ordered = Boolean(numbered);
      if (list && list.ordered !== ordered) flushList();
      list ??= { ordered, items: [] };
      list.items.push((bullet ?? numbered)![1]!);
    } else if (!line.trim()) {
      flushPara();
      flushList();
    } else {
      flushList();
      para.push(line.trim());
    }
  }
  flushPara();
  flushList();
  return <>{out}</>;
}

function inline(text: string): ReactNode {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/g).map((piece, i) => {
    if (piece.startsWith("`") && piece.endsWith("`") && piece.length > 1) {
      return <code key={i}>{piece.slice(1, -1)}</code>;
    }
    if (piece.startsWith("**") && piece.endsWith("**") && piece.length > 3) {
      return <strong key={i}>{piece.slice(2, -2)}</strong>;
    }
    return <Fragment key={i}>{piece}</Fragment>;
  });
}
