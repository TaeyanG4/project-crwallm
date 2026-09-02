"use client";

import { cellText, columnsOf } from "@/lib/format";

/**
 * Extracted rows, as a table.
 *
 * The columns come from the data rather than from the recipe: records also
 * arrive from ad-hoc extraction and from the chat, and a table that only knew
 * about recipe fields would silently drop the rest.
 */
export function RecordTable({ rows }: { rows: Record<string, unknown>[] }) {
  const columns = columnsOf(rows);

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
        <p>레코드가 없습니다.</p>
        <p className="mt-1 text-xs">
          가져온 페이지는 <strong>페이지</strong> 탭에 있습니다 — 전부 200이면 레시피가 빗나간
          것이고, 4xx가 많으면 시드나 범위 문제입니다.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border">
      <table className="w-full text-sm">
        <thead className="bg-muted/40 text-left">
          <tr>
            {columns.map((column) => (
              <th key={column} className="whitespace-nowrap px-3 py-2 font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y">
          {rows.map((row, index) => (
            <tr key={index} className="align-top hover:bg-muted/30">
              {columns.map((column) => {
                const text = cellText(row[column]);
                const isLink = /^https?:\/\//.test(text);
                return (
                  <td key={column} className="max-w-md px-3 py-1.5">
                    {isLink ? (
                      <a
                        href={text}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="text-running underline-offset-2 hover:underline"
                      >
                        {text}
                      </a>
                    ) : (
                      <span className="line-clamp-3 whitespace-pre-wrap">{text}</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
