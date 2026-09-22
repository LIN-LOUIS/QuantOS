export function DataTable({ rows, columns }: { rows: Array<Record<string, unknown>>; columns?: string[] }) {
  const fields = columns ?? (rows[0] ? Object.keys(rows[0]) : []);
  return <div className="table-wrap"><table><thead><tr>{fields.map((field) => <th key={field} scope="col">{field}</th>)}</tr></thead>
    <tbody>{rows.map((row, index) => <tr key={index}>{fields.map((field) => <td key={field}>{formatValue(row[field])}</td>)}</tr>)}</tbody></table></div>;
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
