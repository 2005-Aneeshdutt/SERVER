import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { IconSearch } from "./icons";
import { Kbd } from "./ui";

export interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon?: ReactNode;
  keywords?: string;
  run: () => void;
}

/** Every whitespace-separated term must appear somewhere in the label, group or keywords. */
function matches(cmd: Command, query: string): boolean {
  const hay = `${cmd.label} ${cmd.group} ${cmd.keywords ?? ""} ${cmd.hint ?? ""}`.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => hay.includes(term));
}

export function CommandPalette({
  open,
  onClose,
  commands,
}: {
  open: boolean;
  onClose: () => void;
  commands: Command[];
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const restore = useRef<HTMLElement | null>(null);
  const listId = useId();

  const results = useMemo(() => commands.filter((c) => matches(c, query)), [commands, query]);

  useEffect(() => {
    if (!open) return;
    restore.current = document.activeElement as HTMLElement | null;
    setQuery("");
    setActive(0);
    const t = window.setTimeout(() => input.current?.focus(), 0);
    return () => {
      window.clearTimeout(t);
      restore.current?.focus?.();
    };
  }, [open]);

  useEffect(() => setActive(0), [query]);

  useEffect(() => {
    list.current
      ?.querySelector<HTMLElement>(`[data-index="${active}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!open) return null;

  const run = (cmd: Command | undefined) => {
    if (!cmd) return;
    onClose();
    cmd.run();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(results.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(results[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "Tab") {
      e.preventDefault(); // keep focus inside the dialog
    }
  };

  let lastGroup = "";
  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={onKey}
      >
        <div className="palette__search">
          <IconSearch size={16} />
          <input
            ref={input}
            className="palette__input"
            placeholder="Type a command or search…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-activedescendant={results[active] ? `${listId}-${active}` : undefined}
            aria-autocomplete="list"
            spellCheck={false}
          />
          <Kbd>Esc</Kbd>
        </div>
        <div className="palette__list" role="listbox" id={listId} ref={list}>
          {results.length === 0 && <p className="palette__empty">No commands match “{query}”.</p>}
          {results.map((cmd, i) => {
            const header = cmd.group !== lastGroup ? cmd.group : null;
            lastGroup = cmd.group;
            return (
              <div key={cmd.id}>
                {header && <div className="palette__group">{header}</div>}
                <div
                  id={`${listId}-${i}`}
                  data-index={i}
                  role="option"
                  aria-selected={i === active}
                  className={`palette__item ${i === active ? "is-active" : ""}`}
                  onMouseMove={() => setActive(i)}
                  onClick={() => run(cmd)}
                >
                  <span className="palette__icon">{cmd.icon}</span>
                  <span className="palette__label">{cmd.label}</span>
                  {cmd.hint && <span className="palette__hint">{cmd.hint}</span>}
                </div>
              </div>
            );
          })}
        </div>
        <div className="palette__foot">
          <span>
            <Kbd>↑</Kbd> <Kbd>↓</Kbd> navigate
          </span>
          <span>
            <Kbd>Enter</Kbd> run
          </span>
          <span className="palette__foot-right">
            <Kbd>Ctrl</Kbd> <Kbd>K</Kbd> toggle
          </span>
        </div>
      </div>
    </div>
  );
}
