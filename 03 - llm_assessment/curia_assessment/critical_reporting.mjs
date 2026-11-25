// Native review view for the integrated critical study. No model calls.
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";
const data = JSON.parse(await fs.readFile(process.argv[2], "utf8"));
const workbook = Workbook.create();
const audit = workbook.worksheets.add("Formalisation audit");
const details = workbook.worksheets.add("Witness details");
const summary = workbook.worksheets.add("Summary");
const text = v => v == null ? "" : String(v);
const literal = v => text(v).startsWith("=") ? "'" + v : v;
const col = n => { let s=""; for(let v=n+1;v;v=Math.floor((v-1)/26)) s=String.fromCharCode(65+(v-1)%26)+s; return s; };
const detailFields = ["rule_id","model","witness_index","component","value"];
const summaryFields = ["model","domain","planned","first_Agreement","first_Disagreement","first_Undetermined","latest_Agreement","latest_Disagreement","latest_Undetermined","first_unavailable","latest_unavailable","formula_consistent_D"];
const summaries = data.summaries.map(row => ({...row,...Object.fromEntries(["first","latest"].flatMap(view => Object.entries(row[view]).map(([k,v])=>[`${view}_${k}`,v])))}));
const structured = new Set(["logical_flags","format_notes","scope_conditions","mapping_assumptions","missing_information","counterexamples","witness_checks"]);
function matrix(fields, rows) { return [fields, ...rows.map(row=>fields.map(f=>row[f]??""))]; }
function populate(sheet,fields,rows,freeze,tableName) {
  const values=matrix(fields,rows).map(row=>row.map(literal));
  const range=sheet.getRangeByIndexes(0,0,values.length,fields.length);
  range.values=values; range.setNumberFormat("@");
  range.format.font={name:"Arial",size:11,color:"#172B3A"}; range.format.wrapText=true; range.format.verticalAlignment="top";
  sheet.showGridLines=false;
  for(let i=0;i<fields.length;i++) {
    const field=fields[i]; const column=sheet.getRangeByIndexes(0,i,values.length,1);
    column.format.columnWidth=field==="model"?31:field==="declare"?60:field==="requirement"?75:field.includes("justification")?85:field==="value"?110:structured.has(field)?35:field==="component"?40:field==="rule_id"?12:field==="expert_notes"?65:27;
    if(structured.has(field)) column.format.wrapText=false;
  }
  if(rows.length) {
    sheet.tables.add(`A1:${col(fields.length-1)}${values.length}`,true,tableName).showFilterButton=true;
    sheet.getRangeByIndexes(1,0,rows.length,fields.length).format.autofitRows();
    for(let r=1;r<values.length;r++) { const row=sheet.getRangeByIndexes(r,0,1,fields.length); row.format.rowHeight=Math.min(409,row.format.rowHeight+6); }
  }
  const header=sheet.getRangeByIndexes(0,0,1,fields.length);
  header.format.fill="#243746"; header.format.font={name:"Arial",size:11,bold:true,color:"#FFFFFF"};
  header.format.horizontalAlignment="center";header.format.verticalAlignment="center";header.format.wrapText=true;header.format.rowHeight=48;
  if(freeze) { sheet.freezePanes.freezeRows(1);sheet.freezePanes.freezeColumns(freeze); }
  return values;
}
populate(audit,data.fields,data.rows,2,"FormalisationAudits");
populate(details,detailFields,data.details,2,"HypotheticalWitnesses");
populate(summary,summaryFields,summaries,0,"FormalisationSummary");
const colours={Agreement:{fill:"#C6EFCE",font:{color:"#006100"}},Disagreement:{fill:"#FFC7CE",font:{color:"#9C0006"}},Undetermined:{fill:"#FFEB9C",font:{color:"#7F6000"}}};
for(const field of ["first_in_scope","latest_in_scope","first_ungated","latest_ungated","expert_outcome"]) {
  const i=data.fields.indexOf(field);
  for(const [label,format] of Object.entries(colours)) audit.getRangeByIndexes(1,i,data.rows.length,1).conditionalFormats.addCustom(`${col(i)}2="${label}"`,format);
}
const expertIndex=data.fields.indexOf("expert_outcome");
audit.getRangeByIndexes(1,expertIndex,data.rows.length,1).dataValidation={rule:{type:"list",values:["Agreement","Disagreement","Undetermined"]}};
const notes=["First labels are reported availability, not necessarily structural validity. Latest counts require a structurally valid response.","This is a rule-level audit, not a case violation table. A hypothetical mismatch does not reclassify an actual case.","Formula-consistent D certifies formula/witness mechanics only. Textual and expert validation remains pending.","expert_outcome is your PRIMARY in-scope adequacy judgement. Use expert_notes also for the secondary ungated assessment.","Combined costs are in study_resources.csv; each retained API attempt is counted once, not once per rule/head."];
for(let i=0;i<notes.length;i++) {const cell=summary.getRangeByIndexes(summaries.length+3+i,0,1,1);cell.values=[[notes[i]]];cell.format.wrapText=false;}
workbook.recalculate();
await (await SpreadsheetFile.exportXlsx(workbook)).save(data.output);
const saved=await SpreadsheetFile.importXlsx(await FileBlob.load(data.output));
for(const [index,fields,rows] of [[0,data.fields,data.rows],[1,detailFields,data.details],[2,summaryFields,summaries]]) {
  const expected=matrix(fields,rows); const actual=saved.worksheets.getItemAt(index).getRangeByIndexes(0,0,expected.length,fields.length).values;
  for(let r=0;r<expected.length;r++) for(let c=0;c<fields.length;c++) {
    const want=text(expected[r][c]),got=text(actual[r][c]);
    if(got!==want && !(want.startsWith("=")&&got==="'"+want)) throw new Error(`Native critical workbook parity failed at ${index}:${col(c)}${r+1}`);
  }
}
await fs.mkdir(data.previews,{recursive:true});
const longest=data.rows.reduce((best,row,i)=>row.in_scope_justification.length>data.rows[best].in_scope_justification.length?i:best,0)+2;
for(const [name,sheetName,range] of [["audit","Formalisation audit","G1:L4"],["secondary","Formalisation audit","M1:Q4"],["longest","Formalisation audit",`H${longest}:L${longest}`],["details","Witness details",`A1:E${Math.min(6,data.details.length+1)}`],["summary","Summary",`A1:L${summaries.length+1}`]]) {
  const image=await saved.render({sheetName,range,scale:1,format:"png"});
  await fs.writeFile(path.join(data.previews,name+".png"),new Uint8Array(await image.arrayBuffer()));
}
await fs.writeFile(path.join(data.previews,"native_checks.json"),JSON.stringify({audit_rows:data.rows.length,detail_rows:data.details.length,literal_csv_value_parity:true},null,2));
