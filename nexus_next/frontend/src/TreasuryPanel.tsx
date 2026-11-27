/*
 * الخزينة والبنوك: إدارة حسابات النقدية والبنوك وحركات القبض والصرف والتحويل.
 * تشمل التسوية البنكية ومطابقة كشف البنك مع الحركات المسجلة.
 */
import { useCallback, useEffect, useState } from "react";
import { formatMoney, multiplyDecimalToFixed } from "./formatters";
import { api, treasuryApi, bankReconciliationApi, type BankStatementImportPreview, type Account, type Branch, type FiscalYear, type TreasuryAccount, type TreasuryTransaction, type TreasuryCheque, type BankReconciliation, type BankReconciliationSummary, type Currency, type ExchangeRate, type TreasuryFxRevaluation, type TreasuryLiquiditySummary, type TreasuryLiquidityForecast, type CashCommitment, type CashCommitmentSchedule, type PettyCashFund, type PettyCashExpense, type PettyCashCount, type PettyCashCustodianHandover, type DirectCashFlowReport, type IndirectCashFlowReport } from "./api";

type Props = { companyId: number };
const today = () => new Date().toISOString().slice(0, 10);
const money = (v: string | number) => formatMoney(v);

// شاشة الخزينة والبنوك وإدارة الحسابات والحركات والتسويات البنكية.
export default function TreasuryPanel({companyId}: Props) {
  const [tab,setTab]=useState<"transactions"|"accounts"|"cheques"|"reconciliation"|"liquidity"|"commitments"|"cashflow"|"pettycash">("transactions");
  const [liquidity,setLiquidity]=useState<TreasuryLiquiditySummary|null>(null);
  const [commitments,setCommitments]=useState<CashCommitment[]>([]);
  const [pettyCashFunds,setPettyCashFunds]=useState<PettyCashFund[]>([]);
  const [pettyCashExpenses,setPettyCashExpenses]=useState<PettyCashExpense[]>([]);
  const [pettyFundForm,setPettyFundForm]=useState({branch:"",code:"",name:"",custodian:"",funding_account:"",petty_cash_account:"",authorized_amount:""});
  const [pettyExpenseForm,setPettyExpenseForm]=useState({fund:"",expense_date:today(),description:"",amount:"",expense_account:"",receipt_number:"",vendor_name:""});
  const [pettyActionAmount,setPettyActionAmount]=useState("");
  const [pettyCountActual,setPettyCountActual]=useState("");
  const [pettyCountNotes,setPettyCountNotes]=useState("");
  const [pettyReceiptFile,setPettyReceiptFile]=useState<File|null>(null);
  const [pettyReviewNote,setPettyReviewNote]=useState("");
  const [pettyCountCounterAccount,setPettyCountCounterAccount]=useState("");
  const [pettyCountRows,setPettyCountRows]=useState<PettyCashCount[]>([]);
  const [pettyHandoverRows,setPettyHandoverRows]=useState<PettyCashCustodianHandover[]>([]);
  const [pettyHandoverCustodian,setPettyHandoverCustodian]=useState("");
  const [pettyHandoverActual,setPettyHandoverActual]=useState("");
  const [pettyHandoverNotes,setPettyHandoverNotes]=useState("");
  const [pettyCloseActual,setPettyCloseActual]=useState("");
  const [commitmentSchedules,setCommitmentSchedules]=useState<CashCommitmentSchedule[]>([]);
  const [commitmentForm,setCommitmentForm]=useState({branch:"",commitment_type:"tax" as CashCommitment["commitment_type"],title:"",period_label:"",amount:"",currency_code:"",exchange_rate:"1",due_date:today(),payable_account:"",notes:""});
  const [scheduleForm,setScheduleForm]=useState({branch:"",title:"",amount:"",currency_code:"",exchange_rate:"1",frequency:"monthly" as CashCommitmentSchedule["frequency"],next_due_date:today(),end_date:"",payable_account:"",notes:""});
  const [scheduleThrough,setScheduleThrough]=useState(today());
  const [paymentForms,setPaymentForms]=useState<Record<number,{amount:string;journal_entry:string;payment_date:string}>>({});
  const [cashFlowMethod,setCashFlowMethod]=useState<"direct"|"indirect">("direct");
  const [cashFlowDateFrom,setCashFlowDateFrom]=useState(()=>`${new Date().getFullYear()}-01-01`);
  const [cashFlowDateTo,setCashFlowDateTo]=useState(today());
  const [cashFlowBranch,setCashFlowBranch]=useState("");
  const [directCashFlow,setDirectCashFlow]=useState<DirectCashFlowReport|null>(null);
  const [indirectCashFlow,setIndirectCashFlow]=useState<IndirectCashFlowReport|null>(null);
  const [cashFlowLoading,setCashFlowLoading]=useState(false);

  const [forecast,setForecast]=useState<TreasuryLiquidityForecast|null>(null);
  const [forecastDays,setForecastDays]=useState(90);
  const [accounts,setAccounts]=useState<TreasuryAccount[]>([]);
  const [transactions,setTransactions]=useState<TreasuryTransaction[]>([]);
  const [cheques,setCheques]=useState<TreasuryCheque[]>([]);
  const [chequeSettlementIds,setChequeSettlementIds]=useState<Record<number,string>>({});
  const [chequeForm,setChequeForm]=useState({branch:"",fiscal_year:"",bank_account:"",direction:"incoming" as "incoming"|"outgoing",cheque_number:"",counterparty:"",amount:"",issue_date:today(),due_date:today(),treasury_transaction:"",notes:""});
  const [branches,setBranches]=useState<Branch[]>([]);
  const [years,setYears]=useState<FiscalYear[]>([]);
  const [gl,setGl]=useState<Account[]>([]);
  const [currencies,setCurrencies]=useState<Currency[]>([]);
  const [exchangeRates,setExchangeRates]=useState<ExchangeRate[]>([]);
  const [baseCurrencyCode,setBaseCurrencyCode]=useState("");
  const [reval,setReval]=useState({account:"",fiscal_year:"",revaluation_date:today(),rate_date:today()});
  const [reversalDate,setReversalDate]=useState(today());
  const [fxHistory,setFxHistory]=useState<TreasuryFxRevaluation[]>([]);
  const [fxHistoryAccount,setFxHistoryAccount]=useState("");
  const [reconciliations,setReconciliations]=useState<BankReconciliation[]>([]);
  const [selectedRecon,setSelectedRecon]=useState("");
  const [reopenReason,setReopenReason]=useState("");
  const [summary,setSummary]=useState<BankReconciliationSummary|null>(null);
  const [loading,setLoading]=useState(true), [busy,setBusy]=useState(false), [error,setError]=useState("");
  const [accountSearch,setAccountSearch]=useState("");
  const [transactionSearch,setTransactionSearch]=useState("");
  const [acc,setAcc]=useState({branch:"",account:"",kind:"cash",code:"",name_ar:"",bank_name:"",account_number:"",iban:"",currency_code:"",opening_balance:"0",opening_foreign_amount:"0",opening_exchange_rate:"",opening_exchange_rate_date:today()});
  const [doc,setDoc]=useState({branch:"",fiscal_year:"",source_account:"",destination_account:"",counter_account:"",kind:"receipt",document_number:"",transaction_date:today(),amount:"",transaction_amount:"",exchange_rate:"",exchange_rate_date:today(),description:"",reference:""});
  const [rec,setRec]=useState({bank_account:"",statement_number:"",statement_date:today(),opening_balance:"0",closing_balance:"0"});
  const [line,setLine]=useState({line_number:"1",transaction_date:today(),description:"",reference:"",amount:""});
  const [adjustmentForm,setAdjustmentForm]=useState({counter_account:"",fiscal_year:"",branch:""});
  const [matchIds,setMatchIds]=useState<Record<number,string>>({});
  const [matchAmounts,setMatchAmounts]=useState<Record<number,string>>({});
  const [statementFile,setStatementFile]=useState<File|null>(null);
  const [statementPreview,setStatementPreview]=useState<BankStatementImportPreview|null>(null);
  const load=useCallback(async()=>{
    setLoading(true);setError("");
    try {
      const [a,t,ch,b,y,g,rs,cs,xr,companies,liquid,forecastData,commitmentRows,scheduleRows,pettyFunds,pettyExpenses]=await Promise.all([treasuryApi.accounts(companyId),treasuryApi.transactions(companyId),treasuryApi.cheques(companyId),api.branches(companyId),api.fiscalYears(companyId),api.accounts(companyId),bankReconciliationApi.list(companyId),api.currencies(),api.exchangeRates(companyId),api.companies(),treasuryApi.liquiditySummary(companyId),treasuryApi.liquidityForecast(companyId,forecastDays),treasuryApi.commitments(companyId),treasuryApi.commitmentSchedules(companyId),treasuryApi.pettyCashFunds(companyId),treasuryApi.pettyCashExpenses(companyId)]);
      const companyBase=companies.find(v=>v.id===companyId)?.base_currency_code||"";
      setAccounts(a);setTransactions(t);setCheques(ch);setCommitments(commitmentRows);setCommitmentSchedules(scheduleRows);setPettyCashFunds(pettyFunds);setPettyCashExpenses(pettyExpenses);setBranches(b);setYears(y);setGl(g);setReconciliations(rs);setCurrencies(cs);setExchangeRates(xr);setBaseCurrencyCode(companyBase);setLiquidity(liquid);setForecast(forecastData);
      setAcc(old=>({...old,currency_code:companyBase}));
      setCommitmentForm(old=>({...old,branch:old.branch||String(b[0]?.id||""),currency_code:old.currency_code||companyBase,payable_account:old.payable_account||String(g.find(v=>v.nature==="liability"&&!v.is_group&&v.is_active)?.id||"")}));
      setScheduleForm(old=>({...old,branch:old.branch||String(b[0]?.id||""),currency_code:old.currency_code||companyBase,payable_account:old.payable_account||String(g.find(v=>v.nature==="liability"&&!v.is_group&&v.is_active)?.id||"")}));
      setReval(old=>({...old,account:old.account||String(a.find(v=>v.is_active&&v.currency_code!==companyBase)?.id||""),fiscal_year:old.fiscal_year||String(y.find(v=>v.status==="open")?.id||"")}));
      setDoc(old=>({...old,branch:old.branch||String(b[0]?.id||""),fiscal_year:old.fiscal_year||String(y.find(v=>v.status==="open")?.id||""),source_account:old.source_account||String(a.find(v=>v.is_active)?.id||"")}));
      setRec(old=>({...old,bank_account:old.bank_account||String(a.find(v=>v.kind==="bank"&&v.is_active)?.id||"")}));
      setAdjustmentForm(old=>({...old,fiscal_year:old.fiscal_year||String(y.find(v=>v.status==="open")?.id||""),branch:old.branch||String(b[0]?.id||"")}));
      setChequeForm(old=>({...old,branch:old.branch||String(b[0]?.id||""),fiscal_year:old.fiscal_year||String(y.find(v=>v.status==="open")?.id||""),bank_account:old.bank_account||String(a.find(v=>v.kind==="bank"&&v.is_active&&v.currency_code===companyBase)?.id||"")}));
      setPettyFundForm(old=>({...old,branch:old.branch||String(b[0]?.id||""),funding_account:old.funding_account||String(a.find(v=>v.is_active&&v.currency_code===companyBase)?.id||""),petty_cash_account:old.petty_cash_account||String(a.find(v=>v.is_active&&v.kind==="cash"&&v.currency_code===companyBase&&v.code!==old.funding_account)?.id||"")}));
      setPettyExpenseForm(old=>({...old,fund:old.fund||String(pettyFunds[0]?.id||""),expense_account:old.expense_account||String(g.find(v=>v.nature==="expense"&&!v.is_group&&v.is_active)?.id||"")}));
      setSelectedRecon(old=>old||String(rs[0]?.id||""));
    } catch(e){setError((e as Error).message||"تعذر تحميل بيانات الخزينة.");} finally{setLoading(false);}
  },[companyId,forecastDays]);
  useEffect(()=>{void load();},[load]);
  const refreshSummary=useCallback(async(id:string)=>{if(!id){setSummary(null);return;}try{setSummary(await bankReconciliationApi.summary(Number(id)));}catch(e){setError((e as Error).message||"تعذر تحميل التسوية.");}},[]);
  useEffect(()=>{if(tab==="reconciliation"&&selectedRecon)void refreshSummary(selectedRecon);},[tab,selectedRecon,refreshSummary]);
  const loadCashFlow=useCallback(async()=>{
    if(!cashFlowDateFrom||!cashFlowDateTo){setError("حدد تاريخ بداية ونهاية التقرير.");return;}
    if(cashFlowDateFrom>cashFlowDateTo){setError("تاريخ البداية يجب ألا يتجاوز تاريخ النهاية.");return;}
    setCashFlowLoading(true);setError("");
    try{
      const branchId=cashFlowBranch?Number(cashFlowBranch):undefined;
      if(cashFlowMethod==="direct"){
        setDirectCashFlow(await treasuryApi.directCashFlow(companyId,cashFlowDateFrom,cashFlowDateTo,branchId));
      }else{
        setIndirectCashFlow(await treasuryApi.indirectCashFlow(companyId,cashFlowDateFrom,cashFlowDateTo,branchId));
      }
    }catch(e){setError((e as Error).message||"تعذر تحميل قائمة التدفقات النقدية.");}
    finally{setCashFlowLoading(false);}
  },[companyId,cashFlowDateFrom,cashFlowDateTo,cashFlowBranch,cashFlowMethod]);
  useEffect(()=>{if(tab==="cashflow")void loadCashFlow();},[tab,loadCashFlow]);
  // حفظ حساب خزينة أو بنك جديد ببيانات الفرع والحساب المقابل.
const saveCommitment=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{await treasuryApi.createCommitment({...commitmentForm,company:companyId,branch:commitmentForm.branch?Number(commitmentForm.branch):null,payable_account:Number(commitmentForm.payable_account)});setCommitmentForm(old=>({...old,title:"",period_label:"",amount:"",notes:""}));await load();}catch(err){setError((err as Error).message||"تعذر حفظ الالتزام.");}finally{setBusy(false);}};
  const saveCommitmentSchedule=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{await treasuryApi.createCommitmentSchedule({...scheduleForm,company:companyId,branch:scheduleForm.branch?Number(scheduleForm.branch):null,payable_account:Number(scheduleForm.payable_account),end_date:scheduleForm.end_date||null});setScheduleForm(old=>({...old,title:"",amount:"",notes:""}));await load();}catch(err){setError((err as Error).message||"تعذر حفظ الجدول الدوري.");}finally{setBusy(false);}};
  const toggleCommitmentSchedule=async(item:CashCommitmentSchedule)=>{setBusy(true);setError("");try{await treasuryApi.setCommitmentScheduleActive(item.id,!item.active);await load();}catch(err){setError((err as Error).message||"تعذر تغيير حالة الجدول الدوري.");}finally{setBusy(false);}};
  const generateSchedule=async(id:number)=>{setBusy(true);setError("");try{const result=await treasuryApi.generateCommitmentSchedule(id,scheduleThrough);setError("تم إنشاء "+result.generated+" استحقاق دوري.");await load();}catch(err){setError((err as Error).message||"تعذر توليد الاستحقاقات.");}finally{setBusy(false);}};
  const saveCommitmentPayment=async(id:number)=>{const form=paymentForms[id];if(!form)return;setBusy(true);setError("");try{await treasuryApi.recordCommitmentPayment(id,{amount:form.amount,payment_date:form.payment_date,journal_entry:Number(form.journal_entry)});await load();}catch(err){setError((err as Error).message||"تعذر ربط دفعة الالتزام بالقيد.");}finally{setBusy(false);}};
  const voidCommitment=async(id:number)=>{setBusy(true);setError("");try{await treasuryApi.voidCommitment(id);await load();}catch(err){setError((err as Error).message||"تعذر إلغاء الالتزام.");}finally{setBusy(false);}};
  const saveAcc=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    const foreign=!!acc.currency_code&&acc.currency_code!==baseCurrencyCode;
    let openingBase=acc.opening_balance;
    if(foreign&&Number(acc.opening_foreign_amount)>0){
      if(Number(acc.opening_exchange_rate)<=0||!acc.opening_exchange_rate_date)throw new Error("أدخل المبلغ الافتتاحي الأجنبي وسعر الصرف وتاريخه.");
      if(!exchangeRates.some(v=>v.currency_code===acc.currency_code&&v.rate_date===acc.opening_exchange_rate_date&&Number(v.rate)===Number(acc.opening_exchange_rate)))throw new Error("سعر صرف الرصيد الافتتاحي غير موجود ضمن الأسعار المعتمدة.");
      openingBase=multiplyDecimalToFixed(acc.opening_foreign_amount,acc.opening_exchange_rate,4);
    }
    await treasuryApi.createAccount({company:companyId,branch:acc.branch?Number(acc.branch):null,account:Number(acc.account),kind:acc.kind,code:acc.code,name_ar:acc.name_ar,bank_name:acc.bank_name,account_number:acc.account_number,iban:acc.iban,currency_code:acc.currency_code||baseCurrencyCode,opening_balance:openingBase,opening_foreign_amount:foreign?acc.opening_foreign_amount:"0",opening_exchange_rate:foreign&&Number(acc.opening_foreign_amount)>0?acc.opening_exchange_rate:null,opening_exchange_rate_date:foreign&&Number(acc.opening_foreign_amount)>0?acc.opening_exchange_rate_date:null});
    setAcc({branch:"",account:"",kind:"cash",code:"",name_ar:"",bank_name:"",account_number:"",iban:"",currency_code:baseCurrencyCode,opening_balance:"0",opening_foreign_amount:"0",opening_exchange_rate:"",opening_exchange_rate_date:today()});await load();
  }catch(e){setError((e as Error).message||"تعذر حفظ الحساب.");}finally{setBusy(false);}};
  // تسجيل مستند حركة نقدية أو بنكية حسب نوع العملية المختار.
const saveDoc=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    const source=accounts.find(a=>a.id===Number(doc.source_account));
    const foreign=!!source&&source.currency_code!==baseCurrencyCode;
    const currency=foreign?currencies.find(v=>v.code===source?.currency_code):undefined;
    if(foreign&&(!currency||Number(doc.transaction_amount)<=0||Number(doc.exchange_rate)<=0||!doc.exchange_rate_date))throw new Error("أدخل المبلغ الأجنبي وسعر الصرف وتاريخه المعتمد.");
    const baseAmount=foreign?multiplyDecimalToFixed(doc.transaction_amount, doc.exchange_rate, 4):doc.amount;
    if(foreign&&!exchangeRates.some(v=>v.currency_code===source?.currency_code&&v.rate_date===doc.exchange_rate_date&&Number(v.rate)===Number(doc.exchange_rate)))throw new Error("سعر الصرف غير موجود ضمن الأسعار المعتمدة للشركة في التاريخ المحدد.");
    await treasuryApi.createTransaction({company:companyId,branch:Number(doc.branch),fiscal_year:Number(doc.fiscal_year),source_account:Number(doc.source_account),destination_account:doc.kind==="transfer"?Number(doc.destination_account):null,counter_account:doc.kind==="transfer"?null:Number(doc.counter_account),kind:doc.kind,document_number:doc.document_number,transaction_date:doc.transaction_date,amount:baseAmount,...(foreign?{transaction_currency:currency!.id,transaction_amount:doc.transaction_amount,exchange_rate:doc.exchange_rate,exchange_rate_date:doc.exchange_rate_date}:{}),description:doc.description,reference:doc.reference});
    setDoc(old=>({...old,document_number:"",amount:"",transaction_amount:"",exchange_rate:"",description:"",reference:""}));await load();
  }catch(e){setError((e as Error).message||"تعذر حفظ المستند.");}finally{setBusy(false);}};
  // ترحيل حركة الخزينة المحددة ثم تحديث الأرصدة والحركات.
const loadFxHistory=async()=>{if(!reval.account)return;setBusy(true);setError("");try{setFxHistory(await treasuryApi.fxRevaluations(Number(reval.account)));setFxHistoryAccount(reval.account);}catch(e){setError((e as Error).message||"تعذر تحميل سجل إعادة التقييم.");}finally{setBusy(false);}};
  const submitRevaluation=async(e:React.FormEvent)=>{e.preventDefault();if(!reval.account)return;setBusy(true);setError("");try{const result=await treasuryApi.revalueAccount(Number(reval.account),{fiscal_year:Number(reval.fiscal_year),revaluation_date:reval.revaluation_date,rate_date:reval.rate_date});await load();setError(result.difference==="0.0000"?"تم تسجيل إعادة التقييم دون فرق محاسبي.":`تمت إعادة التقييم؛ فرق العملة ${money(result.difference)} ${baseCurrencyCode}. ${result.journal_number?`القيد ${result.journal_number}`:""}`);setFxHistory(await treasuryApi.fxRevaluations(Number(reval.account)));setFxHistoryAccount(reval.account);}catch(e){setError((e as Error).message||"تعذرت إعادة التقييم.");}finally{setBusy(false);}};
  const reverseRevaluation=async(row:TreasuryFxRevaluation)=>{if(!fxHistoryAccount||!row.can_reverse||!window.confirm(`هل تريد عكس إعادة التقييم بتاريخ ${row.revaluation_date} بقيمة ${money(row.difference)}؟ سيتم إنشاء قيد عكسي وتحديث مركز العملة.`))return;setBusy(true);setError("");try{const result=await treasuryApi.reverseRevaluation(Number(fxHistoryAccount),row.id,reversalDate);await load();setFxHistory(await treasuryApi.fxRevaluations(Number(fxHistoryAccount)));setError(`تم عكس إعادة التقييم. القيد العكسي: ${result.reversal_journal_number}`);}catch(e){setError((e as Error).message||"تعذر عكس إعادة التقييم.");}finally{setBusy(false);}};
  const post=async(id:number)=>{setBusy(true);setError("");try{await treasuryApi.postTransaction(id);await load();}catch(e){setError((e as Error).message||"تعذر الترحيل.");}finally{setBusy(false);}};
  const reverseTransaction=async(row:TreasuryTransaction)=>{const currency=currencies.find(v=>v.id===row.transaction_currency)?.code||"";const amountText=row.transaction_amount?(`المبلغ الأصلي ${money(row.transaction_amount)} ${currency}، والقيمة الدفترية ${money(row.carrying_amount||row.amount)} ${baseCurrencyCode}`):(`القيمة ${money(row.amount)} ${baseCurrencyCode}`);if(!window.confirm(`هل تريد عكس المستند ${row.document_number}؟ ${amountText}. سيتم عكس القيد الأصلي بالكامل، بما فيه فرق العملة المحقق، واستعادة مركز العملة بالقيمة الدفترية الأصلية. يجب عكس أي حركات أو تقييمات أحدث أولًا.`))return;setBusy(true);setError("");try{const result=await treasuryApi.reverseTransaction(row.id,today());await load();setError(`تم عكس المستند ${row.document_number}. القيد العكسي: ${result.reversal_journal_number}`);}catch(e){setError((e as Error).message||"تعذر عكس مستند الخزينة.");}finally{setBusy(false);}};
  // إنشاء جلسة تسوية بنكية مرتبطة بحساب البنك وتاريخ كشف الحساب.
const createRecon=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    const created=await bankReconciliationApi.create({company:companyId,bank_account:Number(rec.bank_account),statement_number:rec.statement_number,statement_date:rec.statement_date,opening_balance:rec.opening_balance,closing_balance:rec.closing_balance});
    await load();setSelectedRecon(String(created.id));setRec(old=>({...old,statement_number:""}));await refreshSummary(String(created.id));
  }catch(e){setError((e as Error).message||"تعذر إنشاء التسوية.");}finally{setBusy(false);}};
  // إضافة سطر من كشف البنك إلى جلسة التسوية الحالية.
const addLine=async(e:React.FormEvent)=>{e.preventDefault();if(!selectedRecon)return;setBusy(true);setError("");try{
    await bankReconciliationApi.addLine(Number(selectedRecon),{line_number:Number(line.line_number),transaction_date:line.transaction_date,description:line.description,reference:line.reference,amount:line.amount});
    setLine(old=>({...old,line_number:String(Number(old.line_number)+1),description:"",reference:"",amount:""}));await refreshSummary(selectedRecon);await load();
  }catch(e){setError((e as Error).message||"تعذر إضافة سطر الكشف.");}finally{setBusy(false);}};
  // معاينة ملف كشف البنك وحصر المكرر قبل السماح بالتأكيد.
const previewStatement=async()=>{if(!selectedRecon||!statementFile)return;setBusy(true);setError("");setStatementPreview(null);try{setStatementPreview(await bankReconciliationApi.previewImport(Number(selectedRecon),statementFile));}catch(e){setError((e as Error).message||"تعذرت معاينة كشف البنك.");}finally{setBusy(false);}};
  // تأكيد استيراد الحركات الجديدة فقط؛ الخادم يعيد فحص التكرار قبل الحفظ.
const confirmStatementImport=async()=>{if(!selectedRecon||!statementFile||!statementPreview)return;setBusy(true);setError("");try{const result=await bankReconciliationApi.confirmImport(Number(selectedRecon),statementFile);setError(result.message);setStatementFile(null);setStatementPreview(null);await refreshSummary(selectedRecon);await load();}catch(e){setError((e as Error).message||"تعذر تأكيد استيراد كشف البنك.");}finally{setBusy(false);}};
  // مطابقة سطر كشف البنك مع حركة خزينة مسجلة.
const match=async(lineId:number)=>{if(!selectedRecon||!matchIds[lineId]||!matchAmounts[lineId])return;setBusy(true);setError("");try{await bankReconciliationApi.match(Number(selectedRecon),lineId,Number(matchIds[lineId]),matchAmounts[lineId]);setMatchAmounts(old=>({...old,[lineId]:""}));await refreshSummary(selectedRecon);}catch(e){setError((e as Error).message||"تعذرت المطابقة.");}finally{setBusy(false);}};
  // إنشاء مسودة سند قبض/صرف مقابل رسوم البنك أو الفوائد أو أي حركة غير مسجلة.
const createAdjustment=async(lineId:number)=>{if(!selectedRecon||!adjustmentForm.counter_account||!adjustmentForm.fiscal_year)return;setBusy(true);setError("");try{const result=await bankReconciliationApi.recordAdjustment(Number(selectedRecon),{line_id:lineId,counter_account:Number(adjustmentForm.counter_account),fiscal_year:Number(adjustmentForm.fiscal_year),...(adjustmentForm.branch?{branch:Number(adjustmentForm.branch)}:{})});setError(`${result.message} رقم المستند: ${result.transaction.document_number}`);await load();await refreshSummary(selectedRecon);}catch(e){setError((e as Error).message||"تعذر إنشاء سند التسوية.");}finally{setBusy(false);}};
  // إلغاء المطابقة الحالية لسطر كشف البنك.
const unmatch=async(lineId:number)=>{if(!selectedRecon)return;setBusy(true);setError("");try{await bankReconciliationApi.unmatch(Number(selectedRecon),lineId);await refreshSummary(selectedRecon);}catch(e){setError((e as Error).message||"تعذر فك المطابقة.");}finally{setBusy(false);}};
  // اعتماد التسوية البنكية بعد استكمال المطابقات والتحقق من الفروق.
const finalize=async()=>{if(!selectedRecon)return;setBusy(true);setError("");try{await bankReconciliationApi.reconcile(Number(selectedRecon));await load();await refreshSummary(selectedRecon);}catch(e){setError((e as Error).message||"تعذر اعتماد التسوية.");}finally{setBusy(false);}};
  const reopen=async()=>{if(!selectedRecon)return;if(reopenReason.trim().length<10){setError("اكتب سبب إعادة الفتح (10 أحرف على الأقل).");return;}setBusy(true);setError("");try{await bankReconciliationApi.reopen(Number(selectedRecon),reopenReason.trim());setReopenReason("");await load();await refreshSummary(selectedRecon);setError("تمت إعادة فتح التسوية؛ راجع المطابقات قبل اعتمادها مرة أخرى.");}catch(e){setError((e as Error).message||"تعذرت إعادة فتح التسوية.");}finally{setBusy(false);}};

  const saveCheque=async(e:React.FormEvent)=>{e.preventDefault();setBusy(true);setError("");try{
    await treasuryApi.createCheque({company:companyId,branch:Number(chequeForm.branch),fiscal_year:Number(chequeForm.fiscal_year),bank_account:Number(chequeForm.bank_account),direction:chequeForm.direction,cheque_number:chequeForm.cheque_number,counterparty:chequeForm.counterparty,amount:chequeForm.amount,issue_date:chequeForm.issue_date,due_date:chequeForm.due_date,...(chequeForm.direction==="outgoing"?{treasury_transaction:Number(chequeForm.treasury_transaction)}:{}),notes:chequeForm.notes});
    setChequeForm(old=>({...old,cheque_number:"",counterparty:"",amount:"",treasury_transaction:"",notes:""}));await load();
  }catch(e){setError((e as Error).message||"تعذر تسجيل الشيك.");}finally{setBusy(false);}};
  const runChequeAction=async(row:TreasuryCheque,action:"deposit"|"under_collection"|"present"|"clear"|"bounce"|"cancel")=>{setBusy(true);setError("");try{
    let payload:Record<string,unknown>={};
    if(action==="deposit")payload={deposit_date:today()};
    if(action==="clear"){const linked=row.direction==="outgoing"?row.treasury_transaction:Number(chequeSettlementIds[row.id]||0);if(!linked)throw new Error("اختر سند الخزينة البنكي المرحّل المطابق أولًا.");payload={clearing_date:today(),treasury_transaction:linked};}
    if(action==="bounce"){const reason=window.prompt("سبب ارتداد الشيك","شيك مرتد من البنك");if(reason===null)return;payload={bounce_date:today(),reason};}
    await treasuryApi.chequeAction(row.id,action,payload);await load();
  }catch(e){setError((e as Error).message||"تعذر تنفيذ إجراء الشيك.");}finally{setBusy(false);}};
  const active=accounts.filter(a=>a.is_active);
  const visibleAccounts=accounts.filter(a=>`${a.code} ${a.name_ar} ${a.bank_name||""} ${a.account_number||""} ${a.iban||""}`.toLocaleLowerCase("ar").includes(accountSearch.trim().toLocaleLowerCase("ar")));
  const visibleTransactions=transactions.filter(t=>`${t.document_number} ${t.transaction_date} ${t.kind} ${t.source_name} ${t.destination_name||""} ${t.journal_number||""} ${t.status}`.toLocaleLowerCase("ar").includes(transactionSearch.trim().toLocaleLowerCase("ar")));
  const bankAccounts=active.filter(a=>a.kind==="bank");
  // حساب الرصيد الجاري لحساب الخزينة من الرصيد الافتتاحي والحركات المرحلة.
const balance=(id:number)=>{const account=accounts.find(a=>a.id===id);if(account&&account.currency_code!==baseCurrencyCode)return Number(account.fx_position_foreign_amount||0);return transactions.filter(t=>t.status==="posted").reduce((n,t)=>n+(t.source_account===id?(t.kind==="receipt"?Number(t.amount):-Number(t.amount)):t.kind==="transfer"&&t.destination_account===id?Number(t.amount):0),Number(account?.opening_balance||0));};
  const currentRecon=reconciliations.find(r=>String(r.id)===selectedRecon);
  // تحديد الحركات المرحلة المرشحة لمطابقة مبلغ سطر كشف البنك.
const eligible=(lineAmount:string)=>summary?.outstanding_transactions.filter(t=>Math.sign(Number(t.amount))===Math.sign(Number(lineAmount))&&Number(t.remaining_amount)>0)||[];
  return <section className="work-panel treasury-panel">
    <div className="work-panel-head"><div><h2>الخزينة والبنوك</h2><p>حسابات مستقلة وسندات وتسويات بنكية مرتبطة بالدفاتر</p></div><span className="coming-soon">{accounts.length} حساب</span></div>
    {error&&<div className="error-banner">{error}</div>}
    <div className="module-tabs"><button className={tab==="liquidity"?"active":""} onClick={()=>setTab("liquidity")}>مركز السيولة</button><button className={tab==="cashflow"?"active":""} onClick={()=>setTab("cashflow")}>قائمة التدفقات النقدية</button><button className={tab==="commitments"?"active":""} onClick={()=>setTab("commitments")}>الالتزامات النقدية</button><button className={tab==="pettycash"?"active":""} onClick={()=>setTab("pettycash")}>العهدة النثرية</button><button className={tab==="transactions"?"active":""} onClick={()=>setTab("transactions")}>سندات القبض والصرف والتحويل</button><button className={tab==="accounts"?"active":""} onClick={()=>setTab("accounts")}>الخزائن والحسابات البنكية</button><button className={tab==="cheques"?"active":""} onClick={()=>setTab("cheques")}>الشيكات الواردة والصادرة</button><button className={tab==="reconciliation"?"active":""} onClick={()=>setTab("reconciliation")}>التسوية البنكية</button></div>
    

    {tab==="pettycash"?<>
      <div className="treasury-form">
        <h3 className="form-wide">إنشاء عهدة</h3>
        <label>الفرع<select value={pettyFundForm.branch} onChange={e=>setPettyFundForm(v=>({...v,branch:e.target.value}))}>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>كود العهدة<input value={pettyFundForm.code} onChange={e=>setPettyFundForm(v=>({...v,code:e.target.value}))}/></label>
        <label>اسم العهدة<input value={pettyFundForm.name} onChange={e=>setPettyFundForm(v=>({...v,name:e.target.value}))}/></label>
        <label>رقم مستخدم أمين العهدة<input type="number" min="1" value={pettyFundForm.custodian} onChange={e=>setPettyFundForm(v=>({...v,custodian:e.target.value}))}/></label>
        <label>حساب التمويل<select value={pettyFundForm.funding_account} onChange={e=>setPettyFundForm(v=>({...v,funding_account:e.target.value}))}>{accounts.filter(a=>a.is_active&&a.currency_code===baseCurrencyCode).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>حساب صندوق العهدة<select value={pettyFundForm.petty_cash_account} onChange={e=>setPettyFundForm(v=>({...v,petty_cash_account:e.target.value}))}>{accounts.filter(a=>a.is_active&&a.kind==="cash"&&a.currency_code===baseCurrencyCode).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>قيمة العهدة المعتمدة<input type="number" min="0.01" step="0.01" value={pettyFundForm.authorized_amount} onChange={e=>setPettyFundForm(v=>({...v,authorized_amount:e.target.value}))}/></label>
        <button type="button" disabled={busy} onClick={async()=>{setBusy(true);setError("");try{await treasuryApi.createPettyCashFund({company:companyId,branch:Number(pettyFundForm.branch),code:pettyFundForm.code,name:pettyFundForm.name,custodian:Number(pettyFundForm.custodian),funding_account:Number(pettyFundForm.funding_account),petty_cash_account:Number(pettyFundForm.petty_cash_account),authorized_amount:pettyFundForm.authorized_amount});setPettyFundForm(v=>({...v,code:"",name:"",custodian:"",authorized_amount:""}));await load();}catch(e){setError((e as Error).message||"تعذر إنشاء العهدة.");}finally{setBusy(false);}}}>حفظ العهدة</button>
      </div>
      <div className="data-table-wrap"><h3>العهد المسجلة</h3><table><thead><tr><th>الكود</th><th>العهدة</th><th>أمين العهدة</th><th>حساب الصندوق</th><th>الحد المعتمد</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
        {pettyCashFunds.map(f=><tr key={f.id}><td>{f.code}</td><td>{f.name}</td><td>{f.custodian_name||f.custodian}</td><td>{f.petty_cash_account_name}</td><td>{money(f.authorized_amount)} {baseCurrencyCode}</td><td>{f.status==="active"?"نشطة":f.status==="suspended"?"موقوفة":"مغلقة"}</td><td>{f.status==="active"&&<button type="button" disabled={busy||pettyCloseActual===""||Number(pettyExpenseForm.fund)!==f.id} onClick={async()=>{setBusy(true);setError("");try{await treasuryApi.closePettyCash(f.id,pettyCloseActual);setPettyCloseActual("");await load();}catch(e){setError((e as Error).message||"تعذر إغلاق العهدة.");}finally{setBusy(false);}}}>إغلاق</button>}</td></tr>)}
        {!pettyCashFunds.length&&<tr><td colSpan={7}>لا توجد عهد مسجلة.</td></tr>}
      </tbody></table></div>
      <div className="treasury-form">
        <h3 className="form-wide">تسجيل مصروف عهدة</h3>
        <label>العهدة<select value={pettyExpenseForm.fund} onChange={e=>setPettyExpenseForm(v=>({...v,fund:e.target.value}))}>{pettyCashFunds.map(f=><option key={f.id} value={f.id}>{f.code} · {f.name}</option>)}</select></label>
        <label>تاريخ المصروف<input type="date" value={pettyExpenseForm.expense_date} onChange={e=>setPettyExpenseForm(v=>({...v,expense_date:e.target.value}))}/></label>
        <label>البيان<input value={pettyExpenseForm.description} onChange={e=>setPettyExpenseForm(v=>({...v,description:e.target.value}))}/></label>
        <label>المبلغ<input type="number" min="0.01" step="0.01" value={pettyExpenseForm.amount} onChange={e=>setPettyExpenseForm(v=>({...v,amount:e.target.value}))}/></label>
        <label>حساب المصروف<select value={pettyExpenseForm.expense_account} onChange={e=>setPettyExpenseForm(v=>({...v,expense_account:e.target.value}))}>{gl.filter(a=>a.nature==="expense"&&!a.is_group&&a.is_active).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>رقم الإيصال<input value={pettyExpenseForm.receipt_number} onChange={e=>setPettyExpenseForm(v=>({...v,receipt_number:e.target.value}))}/></label>
        <label>المورد / الجهة<input value={pettyExpenseForm.vendor_name} onChange={e=>setPettyExpenseForm(v=>({...v,vendor_name:e.target.value}))}/></label>
        <label>صورة / ملف الإيصال<input type="file" accept=".pdf,.jpg,.jpeg,.png" onChange={e=>setPettyReceiptFile(e.target.files?.[0]||null)}/></label>
        <button type="button" disabled={busy||!pettyExpenseForm.fund} onClick={async()=>{setBusy(true);setError("");try{const data=new FormData();data.append("fund",pettyExpenseForm.fund);data.append("expense_date",pettyExpenseForm.expense_date);data.append("description",pettyExpenseForm.description);data.append("amount",pettyExpenseForm.amount);data.append("expense_account",pettyExpenseForm.expense_account);data.append("receipt_number",pettyExpenseForm.receipt_number);data.append("vendor_name",pettyExpenseForm.vendor_name);if(pettyReceiptFile)data.append("receipt_upload",pettyReceiptFile);const created=await treasuryApi.createPettyCashExpense(data);await treasuryApi.submitPettyCashExpense(created.id);setPettyExpenseForm(v=>({...v,description:"",amount:"",receipt_number:"",vendor_name:""}));setPettyReceiptFile(null);await load();}catch(e){setError((e as Error).message||"تعذر حفظ المصروف وإرساله للاعتماد.");}finally{setBusy(false);}}}>حفظ وإرسال للاعتماد</button>
      </div>
      <div className="treasury-form">
        <h3 className="form-wide">إجراءات العهدة</h3>
        <label>العهدة<select value={pettyExpenseForm.fund} onChange={e=>setPettyExpenseForm(v=>({...v,fund:e.target.value}))}>{pettyCashFunds.map(f=><option key={f.id} value={f.id}>{f.code} · {f.name}</option>)}</select></label>
        <label>النقدية الفعلية عند الإغلاق<input type="number" min="0" step="0.01" value={pettyCloseActual} onChange={e=>setPettyCloseActual(e.target.value)}/></label>
        <p className="form-wide">عند الإغلاق، يطابق النظام النقدية الفعلية مع الرصيد الدفتري ويرد المتبقي إلى حساب تمويل العهدة بقيد تحويل.</p>
        <label>مبلغ الصرف / التغذية<input type="number" min="0.01" step="0.01" value={pettyActionAmount} onChange={e=>setPettyActionAmount(e.target.value)}/></label>
        <button type="button" disabled={busy||!pettyExpenseForm.fund||!pettyActionAmount} onClick={async()=>{setBusy(true);setError("");try{await treasuryApi.fundPettyCash(Number(pettyExpenseForm.fund),pettyActionAmount,today());setPettyActionAmount("");await load();}catch(e){setError((e as Error).message||"تعذر صرف العهدة.");}finally{setBusy(false);}}}>صرف العهدة التأسيسية</button>
        <button type="button" disabled={busy||!pettyExpenseForm.fund||!pettyActionAmount} onClick={async()=>{setBusy(true);setError("");try{await treasuryApi.replenishPettyCash(Number(pettyExpenseForm.fund),pettyActionAmount,today());setPettyActionAmount("");await load();}catch(e){setError((e as Error).message||"تعذر تغذية العهدة.");}finally{setBusy(false);}}}>تغذية بعد التسوية</button>
        <label>الرصيد الفعلي عند الجرد<input type="number" min="0" step="0.01" value={pettyCountActual} onChange={e=>setPettyCountActual(e.target.value)}/></label>
        <label>ملاحظات الجرد<input value={pettyCountNotes} onChange={e=>setPettyCountNotes(e.target.value)}/></label>
        <button type="button" disabled={busy||!pettyExpenseForm.fund||pettyCountActual===""} onClick={async()=>{setBusy(true);setError("");try{const result=await treasuryApi.countPettyCash(Number(pettyExpenseForm.fund),{count_date:today(),actual_balance:pettyCountActual,notes:pettyCountNotes});setError("تم تسجيل الجرد وإرساله للاعتماد. فرق العهدة: "+result.variance);setPettyCountRows(await treasuryApi.pettyCashCounts(Number(pettyExpenseForm.fund)));setPettyCountActual("");setPettyCountNotes("");}catch(e){setError((e as Error).message||"تعذر تسجيل الجرد.");}finally{setBusy(false);}}}>تسجيل الجرد</button>
      </div>
      <div className="data-table-wrap"><h3>مصروفات العهدة</h3><label>ملاحظة المراجعة / الرفض<input value={pettyReviewNote} onChange={e=>setPettyReviewNote(e.target.value)} placeholder="سبب الرفض أو ملاحظة الاعتماد"/></label><table><thead><tr><th>التاريخ</th><th>العهدة</th><th>البيان</th><th>الحساب</th><th>المبلغ</th><th>الحالة</th><th>الإيصال</th><th>الإجراء</th><th>قيد التسوية</th></tr></thead><tbody>
        {pettyCashExpenses.map(e=><tr key={e.id}><td>{e.expense_date}</td><td>{e.fund_name}</td><td>{e.description}</td><td>{e.expense_account_code} · {e.expense_account_name}</td><td>{money(e.amount)}</td><td>{e.status==="draft"?"مسودة":e.status==="pending_approval"?"بانتظار الاعتماد":e.status==="approved"?"معتمد":e.status==="rejected"?"مرفوض":e.status==="settled"?"تمت التسوية":"ملغى"}</td><td>{e.receipt_file?<a href={`/api/treasury/petty-cash-expenses/${e.id}/receipt/`}>عرض المرفق</a>:e.receipt_number||"—"}</td><td>{e.status==="pending_approval"?<><button type="button" disabled={busy} onClick={async()=>{setBusy(true);try{await treasuryApi.approvePettyCashExpense(e.id,pettyReviewNote);setPettyReviewNote("");await load();}catch(err){setError((err as Error).message);}finally{setBusy(false);}}}>اعتماد</button><button type="button" disabled={busy||pettyReviewNote.trim().length<10} onClick={async()=>{setBusy(true);try{await treasuryApi.rejectPettyCashExpense(e.id,pettyReviewNote);setPettyReviewNote("");await load();}catch(err){setError((err as Error).message);}finally{setBusy(false);}}}>رفض</button></>:e.status==="rejected"?<button type="button" disabled={busy} onClick={async()=>{setBusy(true);try{await treasuryApi.submitPettyCashExpense(e.id);await load();}catch(err){setError((err as Error).message);}finally{setBusy(false);}}}>إعادة إرسال</button>:"—"}</td><td>{e.settlement_journal_number||"—"}</td></tr>)}
        {!pettyCashExpenses.length&&<tr><td colSpan={9}>لا توجد مصروفات.</td></tr>}
      </tbody></table></div>
      <div className="data-table-wrap"><h3>تسوية المصروفات المعتمدة</h3><p>سيتم تسوية جميع المصروفات المسودة للعهدة المختارة في قيد يومية واحد.</p>
        <button type="button" disabled={busy||!pettyExpenseForm.fund||!pettyCashExpenses.some(e=>e.fund===Number(pettyExpenseForm.fund)&&e.status==="approved")} onClick={async()=>{setBusy(true);setError("");try{const ids=pettyCashExpenses.filter(e=>e.fund===Number(pettyExpenseForm.fund)&&e.status==="approved").map(e=>e.id);const result=await treasuryApi.settlePettyCash(Number(pettyExpenseForm.fund),ids,today());setError("تمت التسوية بالقيد "+result.journal_number+" بإجمالي "+money(result.total_amount));await load();}catch(e){setError((e as Error).message||"تعذرت تسوية المصروفات.");}finally{setBusy(false);}}}>تسوية المصروفات وإنشاء قيد واحد</button>
      </div>
      <div className="treasury-form">
        <h3 className="form-wide">تسليم واستلام العهدة</h3>
        <label>العهدة<select value={pettyExpenseForm.fund} onChange={e=>setPettyExpenseForm(v=>({...v,fund:e.target.value}))}>{pettyCashFunds.map(f=><option key={f.id} value={f.id}>{f.code} · {f.name}</option>)}</select></label>
        <label>رقم المستخدم المستلم<input type="number" min="1" value={pettyHandoverCustodian} onChange={e=>setPettyHandoverCustodian(e.target.value)}/></label>
        <label>الرصيد الفعلي المسلم<input type="number" min="0" step="0.01" value={pettyHandoverActual} onChange={e=>setPettyHandoverActual(e.target.value)}/></label>
        <label>ملاحظات التسليم<input value={pettyHandoverNotes} onChange={e=>setPettyHandoverNotes(e.target.value)}/></label>
        <button type="button" disabled={busy||!pettyExpenseForm.fund||!pettyHandoverCustodian||pettyHandoverActual===""} onClick={async()=>{setBusy(true);try{await treasuryApi.handoverPettyCash(Number(pettyExpenseForm.fund),{new_custodian:Number(pettyHandoverCustodian),handover_date:today(),actual_balance:pettyHandoverActual,notes:pettyHandoverNotes});setPettyHandoverCustodian("");setPettyHandoverActual("");setPettyHandoverNotes("");setPettyHandoverRows(await treasuryApi.pettyCashHandovers(Number(pettyExpenseForm.fund)));await load();}catch(e){setError((e as Error).message||"تعذر تسليم العهدة.");}finally{setBusy(false);}}}>تسجيل محضر التسليم</button>
        <button type="button" disabled={busy||!pettyExpenseForm.fund} onClick={async()=>{setBusy(true);try{setPettyCountRows(await treasuryApi.pettyCashCounts(Number(pettyExpenseForm.fund)));setPettyHandoverRows(await treasuryApi.pettyCashHandovers(Number(pettyExpenseForm.fund)));}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>تحديث سجلات الجرد والتسليم</button>
      </div>
      <div className="data-table-wrap"><h3>محاضر الجرد واعتماد الفروق</h3>
        <label>حساب فرق الجرد<select value={pettyCountCounterAccount} onChange={e=>setPettyCountCounterAccount(e.target.value)}><option value="">اختر الحساب حسب نوع الفرق</option>{gl.filter(a=>a.is_active&&!a.is_group&&(a.nature==="expense"||a.nature==="revenue")).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <table><thead><tr><th>التاريخ</th><th>الرصيد الدفتري</th><th>الفعلي</th><th>الفرق</th><th>الحالة</th><th>الإجراء</th><th>القيد</th></tr></thead><tbody>
          {pettyCountRows.map(row=><tr key={row.id}><td>{row.count_date}</td><td>{money(row.expected_balance)}</td><td>{money(row.actual_balance)}</td><td>{money(row.variance)}</td><td>{row.status==="pending"?"بانتظار الاعتماد":row.status==="approved"?"معتمد":"مرفوض"}</td><td>{row.status==="pending"?<><button type="button" disabled={busy||(Number(row.variance)!==0&&!pettyCountCounterAccount)} onClick={async()=>{setBusy(true);try{await treasuryApi.approvePettyCashCount(row.fund,row.id,pettyCountCounterAccount?Number(pettyCountCounterAccount):undefined,pettyReviewNote);setPettyCountRows(await treasuryApi.pettyCashCounts(row.fund));setPettyReviewNote("");}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>اعتماد الجرد</button><button type="button" disabled={busy||pettyReviewNote.trim().length<10} onClick={async()=>{setBusy(true);try{await treasuryApi.rejectPettyCashCount(row.fund,row.id,pettyReviewNote);setPettyCountRows(await treasuryApi.pettyCashCounts(row.fund));setPettyReviewNote("");}catch(e){setError((e as Error).message);}finally{setBusy(false);}}}>رفض</button></>: "—"}</td><td>{row.adjustment_journal_entry||"—"}</td></tr>)}
          {!pettyCountRows.length&&<tr><td colSpan={7}>لا توجد محاضر جرد معروضة؛ اضغط تحديث سجلات الجرد والتسليم.</td></tr>}
        </tbody></table>
      </div>
      <div className="data-table-wrap"><h3>سجل تسليم العهدة</h3><table><thead><tr><th>التاريخ</th><th>الأمين السابق</th><th>الأمين الجديد</th><th>الرصيد المسلم</th><th>ملاحظات</th></tr></thead><tbody>
        {pettyHandoverRows.map(row=><tr key={row.id}><td>{row.handover_date}</td><td>{row.previous_custodian_name}</td><td>{row.new_custodian_name}</td><td>{money(row.actual_balance)}</td><td>{row.notes||"—"}</td></tr>)}
        {!pettyHandoverRows.length&&<tr><td colSpan={5}>لا توجد محاضر تسليم محملة.</td></tr>}
      </tbody></table></div>
    </>:null}
    {tab==="cashflow"?<>
      <div className="treasury-form">
        <label>طريقة العرض<select value={cashFlowMethod} onChange={e=>setCashFlowMethod(e.target.value as "direct"|"indirect")}><option value="direct">الطريقة المباشرة</option><option value="indirect">الطريقة غير المباشرة</option></select></label>
        <label>من تاريخ<input type="date" value={cashFlowDateFrom} onChange={e=>setCashFlowDateFrom(e.target.value)}/></label>
        <label>إلى تاريخ<input type="date" value={cashFlowDateTo} onChange={e=>setCashFlowDateTo(e.target.value)}/></label>
        <label>الفرع<select value={cashFlowBranch} onChange={e=>setCashFlowBranch(e.target.value)}><option value="">كل الفروع المتاحة</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <button type="button" disabled={cashFlowLoading} onClick={()=>void loadCashFlow()}>{cashFlowLoading?"جارٍ التحميل…":"تحديث التقرير"}</button>
      </div>
      {cashFlowMethod==="direct"&&directCashFlow?<>
        <div className="treasury-summary-grid">
          <div><small>التدفق التشغيلي</small><strong>{money(directCashFlow.operating_cash_flow)} {baseCurrencyCode}</strong></div>
          <div><small>التدفق الاستثماري</small><strong>{money(directCashFlow.investing_cash_flow)} {baseCurrencyCode}</strong></div>
          <div><small>التدفق التمويلي</small><strong>{money(directCashFlow.financing_cash_flow)} {baseCurrencyCode}</strong></div>
          <div><small>صافي التغير النقدي</small><strong>{money(directCashFlow.net_increase_decrease_in_cash)} {baseCurrencyCode}</strong></div>
          <div><small>رصيد أول الفترة</small><strong>{money(directCashFlow.opening_cash_balance)} {baseCurrencyCode}</strong></div>
          <div><small>رصيد آخر الفترة</small><strong>{money(directCashFlow.closing_cash_balance)} {baseCurrencyCode}</strong></div>
          <div><small>رصيد الأستاذ آخر الفترة</small><strong>{money(directCashFlow.ledger_closing_cash_balance)} {baseCurrencyCode}</strong></div>
          <div><small>فرق المطابقة</small><strong>{money(directCashFlow.reconciliation_difference)} {baseCurrencyCode}</strong></div>
        </div>
        {directCashFlow.sections.map(section=><div className="data-table-wrap" key={section.category}>
          <h3>{section.label}</h3>
          <p>مقبوضات: {money(section.cash_receipts)} · مدفوعات: {money(section.cash_payments)} · الصافي: {money(section.net_cash_flow)} {baseCurrencyCode}</p>
          <table><thead><tr><th>الحساب المقابل</th><th>النوع</th><th>مقبوضات</th><th>مدفوعات</th></tr></thead><tbody>
            {section.details.map((row,index)=><tr key={row.counter_account_id||index}><td>{row.counter_account_code||""} · {row.counter_account_name}</td><td>{row.kind==="receipt"?"قبض":"صرف"}</td><td>{money(row.inflows)}</td><td>{money(row.outflows)}</td></tr>)}
            {!section.details.length&&<tr><td colSpan={4}>لا توجد حركات في هذا التصنيف.</td></tr>}
          </tbody></table>
        </div>)}
        {!!directCashFlow.uncategorized_counterparty_transactions.length&&<div className="error-banner">هناك {directCashFlow.uncategorized_counterparty_transactions.length} حركة تحتاج مراجعة الحساب المقابل أو تصنيف التدفق النقدي.</div>}
        {!!directCashFlow.non_cash_investing_transactions.length&&<div className="data-table-wrap"><h3>استثمارات غير نقدية (إفصاح منفصل)</h3><table><thead><tr><th>المستند</th><th>الأصل</th><th>المورد</th><th>القيمة</th><th>المسدد نقدًا</th><th>غير المسدد</th></tr></thead><tbody>
          {directCashFlow.non_cash_investing_transactions.map((row,index)=><tr key={row.invoice_number+index}><td>{row.invoice_number}</td><td>{row.asset_name}</td><td>{row.supplier_name}</td><td>{money(row.gross_amount)}</td><td>{money(row.cash_paid_during_period)}</td><td>{money(row.unpaid_at_period_end)}</td></tr>)}
        </tbody></table></div>}
        <p className="form-wide">{directCashFlow.note}</p>
      </>:null}
      {cashFlowMethod==="indirect"&&indirectCashFlow?<>
        <div className="treasury-summary-grid">
          <div><small>صافي الربح</small><strong>{money(indirectCashFlow.net_profit)} {baseCurrencyCode}</strong></div>
          <div><small>تسويات غير نقدية</small><strong>{money(indirectCashFlow.non_cash_adjustments.net)} {baseCurrencyCode}</strong></div>
          <div><small>تدفق تشغيلي</small><strong>{money(indirectCashFlow.operating_cash_flow)} {baseCurrencyCode}</strong></div>
          <div><small>تدفق استثماري</small><strong>{money(indirectCashFlow.investing_cash_flow)} {baseCurrencyCode}</strong></div>
          <div><small>تدفق تمويلي</small><strong>{money(indirectCashFlow.financing_cash_flow)} {baseCurrencyCode}</strong></div>
          <div><small>صافي التغير النقدي</small><strong>{money(indirectCashFlow.net_increase_decrease_in_cash)} {baseCurrencyCode}</strong></div>
          <div><small>رصيد أول الفترة</small><strong>{money(indirectCashFlow.opening_cash_balance)} {baseCurrencyCode}</strong></div>
          <div><small>رصيد آخر الفترة</small><strong>{money(indirectCashFlow.closing_cash_balance)} {baseCurrencyCode}</strong></div>
          <div><small>رصيد الأستاذ آخر الفترة</small><strong>{money(indirectCashFlow.ledger_closing_cash_balance)} {baseCurrencyCode}</strong></div>
          <div><small>فرق المطابقة</small><strong>{money(indirectCashFlow.reconciliation_difference)} {baseCurrencyCode}</strong></div>
        </div>
        <div className="data-table-wrap"><h3>تسويات صافي الربح</h3><table><thead><tr><th>البند</th><th>القيمة</th></tr></thead><tbody>
          <tr><td>الإهلاك</td><td>{money(indirectCashFlow.non_cash_adjustments.depreciation)}</td></tr><tr><td>خسائر استبعاد الأصول</td><td>{money(indirectCashFlow.non_cash_adjustments.disposal_loss)}</td></tr><tr><td>أرباح استبعاد الأصول</td><td>{money(indirectCashFlow.non_cash_adjustments.disposal_gain)}</td></tr><tr><td>صافي التسويات غير النقدية</td><td>{money(indirectCashFlow.non_cash_adjustments.net)}</td></tr>
        </tbody></table></div>
        <div className="data-table-wrap"><h3>تغيرات رأس المال العامل</h3><table><thead><tr><th>الحساب</th><th>رصيد أول الفترة</th><th>رصيد آخر الفترة</th><th>التغير</th><th>أثر التدفق</th></tr></thead><tbody>
          {indirectCashFlow.working_capital.map((row,index)=><tr key={row.role+index}><td>{row.account_code} · {row.account_name}</td><td>{money(row.opening_balance)}</td><td>{money(row.closing_balance)}</td><td>{money(row.change)}</td><td>{money(row.cash_flow_adjustment)}</td></tr>)}
          {!indirectCashFlow.working_capital.length&&<tr><td colSpan={5}>لا توجد حسابات رأس مال عامل مرتبطة بالأدوار.</td></tr>}
        </tbody></table></div>
        {!!indirectCashFlow.missing_account_roles.length&&<div className="error-banner">أدوار حسابات تحتاج ربطًا لإكمال القائمة: {indirectCashFlow.missing_account_roles.join("، ")}</div>}
        <p className="form-wide">{indirectCashFlow.note}</p>
      </>:null}
      {cashFlowLoading&&<p className="form-wide">جارٍ إعداد قائمة التدفقات النقدية…</p>}
    </>:tab==="commitments"?<>
      <div className="form-wide"><strong>سجل الالتزامات النقدية</strong><p>للمبالغ غير الموجودة بالفعل في فواتير العملاء والموردين أو دورة المرتبات. التسجيل هنا لا ينشئ قيدًا محاسبيًا ولا يغير رصيد البنك.</p></div>
      <form className="treasury-form" onSubmit={saveCommitment}>
        <label>نوع الالتزام<select value={commitmentForm.commitment_type} onChange={e=>setCommitmentForm({...commitmentForm,commitment_type:e.target.value as CashCommitment["commitment_type"]})}><option value="tax">ضرائب ورسوم</option><option value="recurring_expense">مصروف دوري</option><option value="friday_overtime">أجور إضافية</option><option value="other">التزامات أخرى</option></select></label>
        <label>اسم الالتزام<input required value={commitmentForm.title} onChange={e=>setCommitmentForm({...commitmentForm,title:e.target.value})}/></label>
        <label>الفترة / البيان<input value={commitmentForm.period_label} onChange={e=>setCommitmentForm({...commitmentForm,period_label:e.target.value})} placeholder="مثال: أكتوبر 2026"/></label>
        <label>تاريخ الاستحقاق<input type="date" required value={commitmentForm.due_date} onChange={e=>setCommitmentForm({...commitmentForm,due_date:e.target.value})}/></label>
        <label>المبلغ<input type="number" min="0.0001" step="0.0001" required value={commitmentForm.amount} onChange={e=>setCommitmentForm({...commitmentForm,amount:e.target.value})}/></label>
        <label>العملة<select value={commitmentForm.currency_code} onChange={e=>setCommitmentForm({...commitmentForm,currency_code:e.target.value,exchange_rate:e.target.value===baseCurrencyCode?"1":commitmentForm.exchange_rate})}>{currencies.filter(x=>x.is_active).map(x=><option key={x.id} value={x.code}>{x.code} · {x.name_ar}</option>)}</select></label>
        {commitmentForm.currency_code!==baseCurrencyCode&&<label>سعر الصرف المعتمد بتاريخ الاستحقاق<input type="number" min="0.0000000001" step="0.0000000001" value={commitmentForm.exchange_rate} onChange={e=>setCommitmentForm({...commitmentForm,exchange_rate:e.target.value})}/></label>}
        <label>حساب الالتزام (خصوم)<select required value={commitmentForm.payable_account} onChange={e=>setCommitmentForm({...commitmentForm,payable_account:e.target.value})}><option value="">اختر حساب الخصوم</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.nature==="liability").map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>الفرع<select value={commitmentForm.branch} onChange={e=>setCommitmentForm({...commitmentForm,branch:e.target.value})}><option value="">كل الفروع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label className="form-wide">ملاحظات<input value={commitmentForm.notes} onChange={e=>setCommitmentForm({...commitmentForm,notes:e.target.value})}/></label>
        <button type="submit" disabled={busy||!commitmentForm.payable_account}>تسجيل الالتزام</button>
      </form>
      <div className="data-table-wrap"><h3>الالتزامات المسجلة</h3><table><thead><tr><th>الاستحقاق</th><th>النوع</th><th>الالتزام</th><th>المبلغ الأساسي</th><th>المسدد</th><th>المتبقي</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
        {commitments.map(item=><tr key={item.id}><td>{item.due_date}</td><td>{item.commitment_type==="tax"?"ضرائب":item.commitment_type==="recurring_expense"?"مصروف دوري":item.commitment_type==="friday_overtime"?"أجور إضافية":"أخرى"}</td><td>{item.title}<small> · {item.payable_account_code}</small></td><td>{money(item.base_amount)} {baseCurrencyCode}</td><td>{money(item.paid_base_amount)}</td><td>{money(item.remaining_base_amount)}</td><td>{item.status==="paid"?"مسدد":item.status==="partially_paid"?"جزئي":item.status==="void"?"ملغي":"مفتوح"}</td><td>
          {item.status!=="void"&&item.status!=="paid"&&<div className="treasury-inline-actions"><input aria-label="مبلغ الدفعة" type="number" min="0.0001" step="0.0001" placeholder="مبلغ" value={paymentForms[item.id]?.amount||""} onChange={e=>setPaymentForms(old=>({...old,[item.id]:{amount:e.target.value,journal_entry:old[item.id]?.journal_entry||"",payment_date:old[item.id]?.payment_date||today()}}))}/><input aria-label="رقم قيد السداد" type="number" min="1" placeholder="رقم القيد" value={paymentForms[item.id]?.journal_entry||""} onChange={e=>setPaymentForms(old=>({...old,[item.id]:{amount:old[item.id]?.amount||"",journal_entry:e.target.value,payment_date:old[item.id]?.payment_date||today()}}))}/><input aria-label="تاريخ السداد" type="date" value={paymentForms[item.id]?.payment_date||today()} onChange={e=>setPaymentForms(old=>({...old,[item.id]:{amount:old[item.id]?.amount||"",journal_entry:old[item.id]?.journal_entry||"",payment_date:e.target.value}}))}/><button type="button" disabled={busy||!paymentForms[item.id]?.amount||!paymentForms[item.id]?.journal_entry} onClick={()=>void saveCommitmentPayment(item.id)}>ربط السداد</button><button type="button" disabled={busy||Number(item.paid_base_amount)>0} onClick={()=>void voidCommitment(item.id)}>إلغاء</button></div>}
        </td></tr>)}
        {!commitments.length&&<tr><td colSpan={8}>لا توجد التزامات مسجلة.</td></tr>}
      </tbody></table></div>
      <form className="treasury-form" onSubmit={saveCommitmentSchedule}>
        <h3 className="form-wide">جدول المصروفات الدورية</h3>
        <label>اسم المصروف<input required value={scheduleForm.title} onChange={e=>setScheduleForm({...scheduleForm,title:e.target.value})}/></label>
        <label>المبلغ<input type="number" min="0.0001" step="0.0001" required value={scheduleForm.amount} onChange={e=>setScheduleForm({...scheduleForm,amount:e.target.value})}/></label>
        <label>العملة<select value={scheduleForm.currency_code} onChange={e=>setScheduleForm({...scheduleForm,currency_code:e.target.value,exchange_rate:e.target.value===baseCurrencyCode?"1":scheduleForm.exchange_rate})}>{currencies.filter(x=>x.is_active).map(x=><option key={x.id} value={x.code}>{x.code}</option>)}</select></label>
        {scheduleForm.currency_code!==baseCurrencyCode&&<label>سعر الصرف<input type="number" min="0.0000000001" step="0.0000000001" value={scheduleForm.exchange_rate} onChange={e=>setScheduleForm({...scheduleForm,exchange_rate:e.target.value})}/></label>}
        <label>التكرار<select value={scheduleForm.frequency} onChange={e=>setScheduleForm({...scheduleForm,frequency:e.target.value as CashCommitmentSchedule["frequency"]})}><option value="monthly">شهري</option><option value="quarterly">ربع سنوي</option><option value="semiannual">نصف سنوي</option><option value="annual">سنوي</option></select></label>
        <label>أول استحقاق<input type="date" required value={scheduleForm.next_due_date} onChange={e=>setScheduleForm({...scheduleForm,next_due_date:e.target.value})}/></label>
        <label>آخر استحقاق (اختياري)<input type="date" value={scheduleForm.end_date} onChange={e=>setScheduleForm({...scheduleForm,end_date:e.target.value})}/></label>
        <label>حساب الالتزام<select required value={scheduleForm.payable_account} onChange={e=>setScheduleForm({...scheduleForm,payable_account:e.target.value})}><option value="">اختر حساب الخصوم</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.nature==="liability").map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>الفرع<select value={scheduleForm.branch} onChange={e=>setScheduleForm({...scheduleForm,branch:e.target.value})}><option value="">كل الفروع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <button type="submit" disabled={busy||!scheduleForm.payable_account}>حفظ الجدول الدوري</button>
      </form>
      <div className="data-table-wrap"><h3>توليد الاستحقاقات المجدولة</h3><label>توليد حتى تاريخ <input type="date" value={scheduleThrough} onChange={e=>setScheduleThrough(e.target.value)}/></label><table><thead><tr><th>المصروف</th><th>التكرار</th><th>الاستحقاق التالي</th><th>الحالة</th><th>إجراء</th></tr></thead><tbody>
        {commitmentSchedules.map(item=><tr key={item.id}><td>{item.title}</td><td>{item.frequency==="monthly"?"شهري":item.frequency==="quarterly"?"ربع سنوي":item.frequency==="semiannual"?"نصف سنوي":"سنوي"}</td><td>{item.next_due_date}</td><td>{item.active?"نشط":"متوقف"}</td><td><button type="button" disabled={busy||!item.active} onClick={()=>void generateSchedule(item.id)}>توليد الاستحقاقات</button><button type="button" disabled={busy} onClick={()=>void toggleCommitmentSchedule(item)}>{item.active?"إيقاف الجدول":"إعادة التفعيل"}</button></td></tr>)}
        {!commitmentSchedules.length&&<tr><td colSpan={5}>لا توجد جداول دورية.</td></tr>}
      </tbody></table></div>
    </>:tab==="liquidity"?<>

      <div className="treasury-summary-grid">
        <div><small>إجمالي السيولة الدفترية ({liquidity?.base_currency||baseCurrencyCode})</small><strong>{money(liquidity?.total_base_balance||0)} {liquidity?.base_currency||baseCurrencyCode}</strong></div>
        <div><small>حسابات الخزائن والبنوك</small><strong>{liquidity?.accounts.length||0}</strong></div>
        <div><small>تاريخ المركز</small><strong>{liquidity?.as_of||today()}</strong></div>
        <div><small>نطاق العرض</small><strong>{liquidity?.branch?branches.find(b=>b.id===liquidity.branch)?.name_ar||"فرع محدد":"الفروع المتاحة"}</strong></div>
      </div>
      <div className="data-table-wrap"><h3>الأرصدة الحالية حسب الحساب</h3><table><thead><tr><th>الحساب</th><th>النوع</th><th>الفرع</th><th>العملة</th><th>الرصيد الاسمي</th><th>القيمة بالعملة الأساسية</th></tr></thead><tbody>
        {(liquidity?.accounts||[]).map(row=><tr key={row.id}><td>{row.code} · {row.name}</td><td>{row.kind==="cash"?"خزينة":"بنك"}</td><td>{row.branch_name}</td><td>{row.currency}</td><td>{money(row.balance)} {row.currency}</td><td>{money(row.base_balance)} {liquidity?.base_currency||baseCurrencyCode}</td></tr>)}
        {!liquidity?.accounts.length&&<tr><td colSpan={6}>لا توجد أرصدة خزائن أو بنوك نشطة.</td></tr>}
      </tbody></table></div>
      <p className="form-wide">{liquidity?.note||"يتم احتساب الرصيد من القيود المرحلة فقط."}</p>
      <section className="treasury-forecast-section">
        <div className="work-panel-head"><div><h3>التدفقات النقدية المتوقعة</h3><p>الرصيد الفعلي مضافًا إليه التحصيلات والمدفوعات المستحقة خلال الفترة</p></div>
          <label>أفق التوقع<select value={forecastDays} onChange={e=>setForecastDays(Number(e.target.value))}><option value={30}>30 يومًا</option><option value={60}>60 يومًا</option><option value={90}>90 يومًا</option><option value={180}>180 يومًا</option></select></label>
        </div>
        <div className="treasury-summary-grid">
          <div><small>تحصيلات العملاء المتوقعة</small><strong>{money(forecast?.expected_collections||0)} {forecast?.base_currency||baseCurrencyCode}</strong></div>
          <div><small>مدفوعات الموردين</small><strong>{money(forecast?.expected_supplier_payments||0)} {forecast?.base_currency||baseCurrencyCode}</strong></div>
          <div><small>المرتبات غير المصروفة</small><strong>{money(forecast?.expected_payroll||0)} {forecast?.base_currency||baseCurrencyCode}</strong></div><div><small>التزامات أخرى</small><strong>{money(forecast?.expected_other_commitments||0)} {forecast?.base_currency||baseCurrencyCode}</strong></div>
          <div><small>صافي التدفق المتوقع</small><strong>{money(forecast?.expected_net||0)} {forecast?.base_currency||baseCurrencyCode}</strong></div>
          <div><small>الرصيد المتوقع بنهاية الفترة</small><strong>{money(forecast?.projected_closing_balance||0)} {forecast?.base_currency||baseCurrencyCode}</strong></div>
        </div>
        <p className="form-wide">{forecast?.note||"يتم احتساب التوقع من الأرصدة المفتوحة فقط."}</p>
        <div className="data-table-wrap"><h3>التوقع حسب تاريخ الاستحقاق</h3><table><thead><tr><th>التاريخ</th><th>تحصيلات</th><th>موردون</th><th>مرتبات</th><th>التزامات أخرى</th><th>إجمالي المدفوعات</th><th>الصافي</th><th>الرصيد المتوقع</th></tr></thead><tbody>
          {(forecast?.daily||[]).map(row=><tr key={row.date}><td>{row.date}</td><td>{money(row.collections)}</td><td>{money(row.supplier_payments)}</td><td>{money(row.payroll)}</td><td>{money(row.other_commitments)}</td><td>{money(row.payments)}</td><td>{money(row.net)}</td><td>{money(row.projected_balance)}</td></tr>)}
          {!forecast?.daily.length&&<tr><td colSpan={8}>لا توجد تحصيلات أو مدفوعات مستحقة خلال الأفق المحدد.</td></tr>}
        </tbody></table></div>
        <div className="data-table-wrap"><h3>المستندات الداخلة في التوقع</h3><table><thead><tr><th>النوع</th><th>الطرف</th><th>المستند</th><th>الاستحقاق</th><th>الرصيد المتبقي</th><th>البيان</th></tr></thead><tbody>
          {(forecast?.items||[]).map((row,index)=><tr key={row.kind+"-"+row.document+"-"+index}><td>{row.kind==="collections"?"تحصيل عميل":row.kind==="supplier_payments"?"سداد مورد":row.kind==="other_commitments"?"التزامات أخرى":"مرتبات"}</td><td>{row.party}</td><td>{row.document}</td><td>{row.due_date}</td><td>{money(row.balance)} {forecast?.base_currency||baseCurrencyCode}</td><td>{row.date_basis}</td></tr>)}
          {!forecast?.items.length&&<tr><td colSpan={6}>لا توجد مستندات مفتوحة ضمن الفترة.</td></tr>}
        </tbody></table></div>
        <ul className="form-wide">{(forecast?.coverage_notes||[]).map((note,index)=><li key={index}>{note}</li>)}</ul>
      </section>
    </>:tab==="accounts"?<>
      <form className="treasury-form" onSubmit={saveAcc}>
        <label>النوع<select value={acc.kind} onChange={e=>setAcc({...acc,kind:e.target.value})}><option value="cash">خزينة نقدية</option><option value="bank">حساب بنكي</option></select></label>
        <label>الكود<input required value={acc.code} onChange={e=>setAcc({...acc,code:e.target.value})}/></label>
        <label>الاسم<input required value={acc.name_ar} onChange={e=>setAcc({...acc,name_ar:e.target.value})}/></label>
        <label>الحساب بدليل الحسابات<select required value={acc.account} onChange={e=>setAcc({...acc,account:e.target.value})}><option value="">اختر الحساب</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>الفرع<select value={acc.branch} onChange={e=>setAcc({...acc,branch:e.target.value})}><option value="">كل الفروع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>العملة<select value={acc.currency_code||baseCurrencyCode} onChange={e=>setAcc({...acc,currency_code:e.target.value})}>{currencies.map(v=><option key={v.id} value={v.code}>{v.code} · {v.name_ar}</option>)}</select></label>
        {acc.currency_code===baseCurrencyCode?<label>الرصيد الافتتاحي ({baseCurrencyCode})<input type="number" min="0" step="0.0001" value={acc.opening_balance} onChange={e=>setAcc({...acc,opening_balance:e.target.value})}/></label>:<><label>الرصيد الافتتاحي ({acc.currency_code})<input type="number" min="0" step="0.0001" value={acc.opening_foreign_amount} onChange={e=>setAcc({...acc,opening_foreign_amount:e.target.value})}/></label><label>سعر الصرف المعتمد<input type="number" min="0" step="0.0000000001" value={acc.opening_exchange_rate} onChange={e=>setAcc({...acc,opening_exchange_rate:e.target.value})}/></label><label>تاريخ سعر الصرف<input type="date" value={acc.opening_exchange_rate_date} onChange={e=>setAcc({...acc,opening_exchange_rate_date:e.target.value})}/></label><p className="form-wide">القيمة الدفترية بالعملة الأساسية: {Number(acc.opening_foreign_amount)>0&&Number(acc.opening_exchange_rate)>0?money(multiplyDecimalToFixed(acc.opening_foreign_amount,acc.opening_exchange_rate,4)):money(0)} {baseCurrencyCode}. يجب أن يكون السعر مسجلًا ومعتمدًا في أسعار الصرف.</p></>}
        {acc.kind==="bank"&&<><label>اسم البنك<input value={acc.bank_name} onChange={e=>setAcc({...acc,bank_name:e.target.value})}/></label><label>رقم الحساب<input value={acc.account_number} onChange={e=>setAcc({...acc,account_number:e.target.value})}/></label><label>IBAN<input value={acc.iban} onChange={e=>setAcc({...acc,iban:e.target.value})}/></label></>}
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy}>حفظ الحساب</button></div>
      </form>
      <form className="treasury-form" onSubmit={submitRevaluation}><h3 className="form-wide">إعادة تقييم أرصدة العملات الأجنبية</h3><label>حساب أجنبي<select required value={reval.account} onChange={e=>setReval({...reval,account:e.target.value})}><option value="">اختر الحساب</option>{active.filter(a=>a.currency_code!==baseCurrencyCode).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar} · {a.currency_code}</option>)}</select></label><label>السنة المالية<select required value={reval.fiscal_year} onChange={e=>setReval({...reval,fiscal_year:e.target.value})}><option value="">اختر السنة المفتوحة</option>{years.filter(y=>y.status==="open").map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label><label>تاريخ إعادة التقييم<input required type="date" value={reval.revaluation_date} onChange={e=>setReval({...reval,revaluation_date:e.target.value})}/></label><label>تاريخ سعر الصرف المعتمد<input required type="date" value={reval.rate_date} onChange={e=>setReval({...reval,rate_date:e.target.value})}/></label><div className="treasury-form-actions"><button className="primary-button" disabled={busy||!reval.account||!reval.fiscal_year}>تنفيذ إعادة التقييم</button><button type="button" className="subtle-button" disabled={busy||!reval.account} onClick={()=>void loadFxHistory()}>عرض سجل إعادة التقييم</button></div><p className="form-wide">يُنشأ قيد فرق عملة غير محقق تلقائيًا، ولا يمكن تكرار التقييم للحساب في التاريخ نفسه. الحسابات ذات الأرصدة الافتتاحية الأجنبية القديمة تتطلب معالجة افتتاحية أولًا.</p></form>
      <div className="data-table-wrap"><h3>سجل إعادة التقييم {fxHistoryAccount?`· ${accounts.find(a=>String(a.id)===fxHistoryAccount)?.name_ar||""}`:""}</h3><label>تاريخ قيد العكس<input type="date" value={reversalDate} onChange={e=>setReversalDate(e.target.value)}/></label><table><thead><tr><th>تاريخ التقييم</th><th>تاريخ السعر</th><th>سعر الصرف</th><th>الرصيد الأجنبي</th><th>القيمة السابقة</th><th>القيمة الجديدة</th><th>الفرق</th><th>الحالة</th><th>القيد الأصلي</th><th>القيد العكسي</th><th>إجراء</th></tr></thead><tbody>{fxHistory.map(row=><tr key={row.id}><td>{row.revaluation_date}</td><td>{row.rate_date}</td><td>{row.rate}</td><td>{money(row.foreign_amount)} {accounts.find(a=>a.id===Number(fxHistoryAccount))?.currency_code}</td><td>{money(row.previous_carrying_amount)} {baseCurrencyCode}</td><td>{money(row.new_carrying_amount)} {baseCurrencyCode}</td><td>{money(row.difference)} {baseCurrencyCode}</td><td>{row.status==="reversed"?"معكوس":row.status==="no_difference"?"دون فرق":"مرحّل"}</td><td>{row.journal_number||"—"}</td><td>{row.reversal_journal_number||"—"}</td><td>{row.can_reverse?<button type="button" className="subtle-button" disabled={busy} onClick={()=>void reverseRevaluation(row)}>عكس التقييم</button>:row.status==="reversed"?"تم العكس":"—"}</td></tr>)}{!fxHistory.length&&<tr><td colSpan={11}>لا يوجد سجل إعادة تقييم لهذا الحساب.</td></tr>}</tbody></table></div>
      <div className="table-toolbar"><input aria-label="البحث في حسابات الخزينة والبنوك" placeholder="ابحث بالكود أو الاسم أو البنك أو IBAN…" value={accountSearch} onChange={e=>setAccountSearch(e.target.value)}/><span className="module-count">{visibleAccounts.length} من {accounts.length} حساب</span></div><div className="data-table-wrap"><table><thead><tr><th>الكود</th><th>الاسم</th><th>النوع</th><th>الحساب المحاسبي</th><th>العملة</th><th>افتتاحي</th><th>الرصيد بالعملة</th><th>القيمة الدفترية</th></tr></thead><tbody>{visibleAccounts.map(a=><tr key={a.id}><td>{a.code}</td><td>{a.name_ar}{a.bank_name?" · "+a.bank_name:""}</td><td>{a.kind==="cash"?"خزينة":"بنك"}</td><td>{a.account_code} · {a.account_name}</td><td>{a.currency_code}</td><td>{money(a.currency_code!==baseCurrencyCode?a.opening_foreign_amount:a.opening_balance)} {a.currency_code}</td><td>{money(balance(a.id))} {a.currency_code}</td><td>{a.currency_code!==baseCurrencyCode?`${money(a.fx_position_carrying_base_amount||0)} ${baseCurrencyCode}`:"—"}</td></tr>)}</tbody></table>{!loading&&!accounts.length&&<div className="table-empty">لا توجد حسابات بعد.</div>}</div>
    </>:tab==="transactions"?<>
      <form className="treasury-form" onSubmit={saveDoc}>
        <label>النوع<select value={doc.kind} onChange={e=>setDoc({...doc,kind:e.target.value,destination_account:"",counter_account:""})}><option value="receipt">سند قبض</option><option value="payment">سند صرف</option><option value="transfer">تحويل</option></select></label>
        <label>رقم المستند<input required value={doc.document_number} onChange={e=>setDoc({...doc,document_number:e.target.value})}/></label>
        <label>التاريخ<input required type="date" value={doc.transaction_date} onChange={e=>setDoc({...doc,transaction_date:e.target.value})}/></label>
        <label>الفرع<select required value={doc.branch} onChange={e=>setDoc({...doc,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>السنة المالية<select required value={doc.fiscal_year} onChange={e=>setDoc({...doc,fiscal_year:e.target.value})}><option value="">السنة المفتوحة</option>{years.filter(y=>y.status==="open").map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label>
        <label>الخزينة / البنك<select required value={doc.source_account} onChange={e=>setDoc({...doc,source_account:e.target.value})}><option value="">اختر الحساب</option>{active.map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        {doc.kind==="transfer"?<label>إلى حساب<select required value={doc.destination_account} onChange={e=>setDoc({...doc,destination_account:e.target.value})}><option value="">الحساب المستلم</option>{active.filter(a=>String(a.id)!==doc.source_account&&a.currency_code===accounts.find(v=>v.id===Number(doc.source_account))?.currency_code).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>:<label>الحساب المقابل<select required value={doc.counter_account} onChange={e=>setDoc({...doc,counter_account:e.target.value})}><option value="">اختر الحساب</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>}
        {accounts.find(a=>a.id===Number(doc.source_account))?.currency_code!==baseCurrencyCode&&doc.source_account?<><label>المبلغ بالعملة الأجنبية<input required type="number" min="0.0001" step="0.0001" value={doc.transaction_amount} onChange={e=>setDoc({...doc,transaction_amount:e.target.value})}/></label><label>سعر الصرف المعتمد<input required type="number" min="0.0000000001" step="0.0000000001" value={doc.exchange_rate} onChange={e=>setDoc({...doc,exchange_rate:e.target.value})}/></label><label>تاريخ سعر الصرف<input required type="date" value={doc.exchange_rate_date} onChange={e=>setDoc({...doc,exchange_rate_date:e.target.value})}/></label><label>القيمة بالعملة الأساسية<input readOnly value={(Number(doc.transaction_amount)>0&&Number(doc.exchange_rate)>0?formatMoney(Number(doc.transaction_amount)*Number(doc.exchange_rate)):"")}/></label></>:<label>المبلغ<input required type="number" min="0.0001" step="0.0001" value={doc.amount} onChange={e=>setDoc({...doc,amount:e.target.value})}/></label>}
        <label className="form-wide">البيان<input required value={doc.description} onChange={e=>setDoc({...doc,description:e.target.value})}/></label>
        <label>المرجع<input value={doc.reference} onChange={e=>setDoc({...doc,reference:e.target.value})}/></label>
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy||!active.length||!years.some(y=>y.status==="open")}>حفظ كمسودة</button></div>
      </form>
      <div className="table-toolbar"><input aria-label="البحث في حركات الخزينة" placeholder="ابحث برقم المستند أو الحساب أو القيد…" value={transactionSearch} onChange={e=>setTransactionSearch(e.target.value)}/><span className="module-count">{visibleTransactions.length} من {transactions.length} مستند</span></div><div className="data-table-wrap"><table><thead><tr><th>المستند</th><th>التاريخ</th><th>النوع</th><th>الحساب</th><th>المبلغ</th><th>الحالة</th><th>القيد</th><th>إجراء</th></tr></thead><tbody>{visibleTransactions.map(t=><tr key={t.id}><td>{t.document_number}</td><td>{t.transaction_date}</td><td>{t.kind==="receipt"?"قبض":t.kind==="payment"?"صرف":"تحويل"}</td><td>{t.source_name}{t.destination_name?" ← "+t.destination_name:""}</td><td>{money(t.amount)} {baseCurrencyCode}{t.transaction_amount?` · ${money(t.transaction_amount)} ${currencies.find(v=>v.id===t.transaction_currency)?.code||""}`:""}</td><td><span className={"status-pill "+t.status}>{t.status==="posted"?"مرحّل":t.status==="reversed"?"معكوس":t.status==="void"?"ملغي":"مسودة"}</span></td><td>{t.journal_number||"—"}</td><td>{t.status==="draft"?<button className="subtle-button" disabled={busy} onClick={()=>void post(t.id)}>ترحيل</button>:t.status==="posted"?<button className="subtle-button" disabled={busy} onClick={()=>void reverseTransaction(t)}>عكس</button>:t.reversal_journal_number?`القيد العكسي ${t.reversal_journal_number}`:"—"}</td></tr>)}</tbody></table>{!loading&&!transactions.length&&<div className="table-empty">لا توجد مستندات حتى الآن.</div>}{transactions.length>0&&!visibleTransactions.length&&<div className="table-empty">لا توجد مستندات مطابقة للبحث.</div>}</div>

    </>:tab==="cheques"?<>
      <form className="treasury-form" onSubmit={saveCheque}>
        <label>نوع الشيك<select value={chequeForm.direction} onChange={e=>setChequeForm({...chequeForm,direction:e.target.value as "incoming"|"outgoing",treasury_transaction:""})}><option value="incoming">شيك وارد من عميل / طرف آخر</option><option value="outgoing">شيك صادر لمورد / طرف آخر</option></select></label>
        <label>رقم الشيك<input required maxLength={80} value={chequeForm.cheque_number} onChange={e=>setChequeForm({...chequeForm,cheque_number:e.target.value})}/></label>
        <label>الطرف المقابل<input required maxLength={180} value={chequeForm.counterparty} onChange={e=>setChequeForm({...chequeForm,counterparty:e.target.value})}/></label>
        <label>الحساب البنكي<select required value={chequeForm.bank_account} onChange={e=>setChequeForm({...chequeForm,bank_account:e.target.value,treasury_transaction:""})}><option value="">اختر الحساب البنكي</option>{bankAccounts.filter(a=>a.currency_code===baseCurrencyCode).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>الفرع<select required value={chequeForm.branch} onChange={e=>setChequeForm({...chequeForm,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>
        <label>السنة المالية<select required value={chequeForm.fiscal_year} onChange={e=>setChequeForm({...chequeForm,fiscal_year:e.target.value})}><option value="">اختر السنة المفتوحة</option>{years.filter(y=>y.status==="open").map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label>
        <label>قيمة الشيك<input required type="number" min="0.0001" step="0.0001" value={chequeForm.amount} onChange={e=>setChequeForm({...chequeForm,amount:e.target.value})}/></label>
        <label>تاريخ الإصدار / الاستلام<input required type="date" value={chequeForm.issue_date} onChange={e=>setChequeForm({...chequeForm,issue_date:e.target.value})}/></label>
        <label>تاريخ الاستحقاق<input required type="date" min={chequeForm.issue_date} value={chequeForm.due_date} onChange={e=>setChequeForm({...chequeForm,due_date:e.target.value})}/></label>
        {chequeForm.direction==="outgoing"&&<label className="form-wide">سند الصرف البنكي المرحّل<select required value={chequeForm.treasury_transaction} onChange={e=>setChequeForm({...chequeForm,treasury_transaction:e.target.value})}><option value="">اختر سند صرف مطابقًا</option>{transactions.filter(t=>t.status==="posted"&&t.kind==="payment"&&t.source_account===Number(chequeForm.bank_account)&&Number(t.amount)===Number(chequeForm.amount)&&!cheques.some(c=>c.treasury_transaction===t.id)).map(t=><option key={t.id} value={t.id}>{t.document_number} · {t.transaction_date} · {money(t.amount)}</option>)}</select><small>الشيك الصادر لا يُسجل دون سند صرف بنكي مرحّل بنفس البنك والقيمة.</small></label>}
        <label className="form-wide">ملاحظات<input maxLength={500} value={chequeForm.notes} onChange={e=>setChequeForm({...chequeForm,notes:e.target.value})}/></label>
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy||!bankAccounts.some(a=>a.currency_code===baseCurrencyCode)||!years.some(y=>y.status==="open")}>تسجيل الشيك</button></div>
      </form>
      <div className="table-toolbar"><span className="module-count">{cheques.length} شيك مسجل</span></div>
      <div className="data-table-wrap"><table><thead><tr><th>الاتجاه</th><th>رقم الشيك</th><th>البنك</th><th>الطرف</th><th>القيمة</th><th>الإصدار</th><th>الاستحقاق</th><th>الحالة</th><th>سند الخزينة</th><th>الإجراء</th></tr></thead><tbody>{cheques.map(row=><tr key={row.id}>
        <td>{row.direction==="incoming"?"وارد":"صادر"}</td><td>{row.cheque_number}</td><td>{row.bank_name}</td><td>{row.counterparty}</td><td>{money(row.amount)} {baseCurrencyCode}</td><td>{row.issue_date}</td><td>{row.due_date}</td>
        <td><span className={"status-pill "+row.status}>{({received:"مستلم",deposited:"مودع بالبنك",under_collection:"تحت التحصيل",issued:"صادر",presented:"مقدم للصرف",cleared:"تم التحصيل/الصرف",bounced:"مرتد",cancelled:"ملغي"} as Record<string,string>)[row.status]}</span></td>
        <td>{row.transaction_number||"—"}</td><td>
          {row.direction==="incoming"&&row.status==="received"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void runChequeAction(row,"deposit")}>إيداع بالبنك</button>}
          {row.direction==="incoming"&&row.status==="deposited"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void runChequeAction(row,"under_collection")}>تحت التحصيل</button>}
          {row.direction==="outgoing"&&row.status==="issued"&&<button type="button" className="subtle-button" disabled={busy} onClick={()=>void runChequeAction(row,"present")}>تقديم للصرف</button>}
          {(row.direction==="incoming"&&["deposited","under_collection"].includes(row.status)||row.direction==="outgoing"&&["issued","presented"].includes(row.status))&&<><select aria-label="سند الخزينة لإثبات التحصيل أو الصرف" value={chequeSettlementIds[row.id]||String(row.treasury_transaction||"")} onChange={e=>setChequeSettlementIds({...chequeSettlementIds,[row.id]:e.target.value})}><option value="">سند التحصيل / الصرف المرحّل</option>{transactions.filter(t=>t.status==="posted"&&t.kind===(row.direction==="incoming"?"receipt":"payment")&&t.source_account===row.bank_account&&Number(t.amount)===Number(row.amount)&&(t.id===row.treasury_transaction||!cheques.some(c=>c.treasury_transaction===t.id))).map(t=><option key={t.id} value={t.id}>{t.document_number} · {t.transaction_date}</option>)}</select><button type="button" className="subtle-button" disabled={busy||!(chequeSettlementIds[row.id]||row.treasury_transaction)} onClick={()=>void runChequeAction(row,"clear")}>تأكيد التحصيل / الصرف</button></>}
          {row.status!=="cleared"&&row.status!=="bounced"&&row.status!=="cancelled"&&<button type="button" className="subtle-button" disabled={busy||(row.direction==="outgoing"&&transactions.find(t=>t.id===row.treasury_transaction)?.status!=="reversed")} onClick={()=>void runChequeAction(row,"bounce")}>تسجيل ارتداد</button>}
          {(row.status==="received"||row.status==="issued")&&<button type="button" className="subtle-button" disabled={busy||(row.direction==="outgoing"&&transactions.find(t=>t.id===row.treasury_transaction)?.status!=="reversed")} onClick={()=>void runChequeAction(row,"cancel")}>إلغاء</button>}
          {["cleared","bounced","cancelled"].includes(row.status)?"—":null}
        </td></tr>)}{!cheques.length&&<tr><td colSpan={10}>لا توجد شيكات مسجلة حتى الآن.</td></tr>}</tbody></table></div>

    </>:<>
      <form className="treasury-form" onSubmit={createRecon}>
        <label>الحساب البنكي<select required value={rec.bank_account} onChange={e=>setRec({...rec,bank_account:e.target.value})}><option value="">اختر البنك</option>{bankAccounts.map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label>
        <label>رقم كشف البنك<input required value={rec.statement_number} onChange={e=>setRec({...rec,statement_number:e.target.value})}/></label>
        <label>تاريخ الكشف<input required type="date" value={rec.statement_date} onChange={e=>setRec({...rec,statement_date:e.target.value})}/></label>
        <label>رصيد أول المدة<input required type="number" step="0.0001" value={rec.opening_balance} onChange={e=>setRec({...rec,opening_balance:e.target.value})}/></label>
        <label>رصيد آخر المدة<input required type="number" step="0.0001" value={rec.closing_balance} onChange={e=>setRec({...rec,closing_balance:e.target.value})}/></label>
        <div className="treasury-form-actions"><button className="primary-button" disabled={busy||!bankAccounts.length}>إنشاء تسوية</button></div>
      </form>
      <div className="treasury-recon-select"><label>التسوية الحالية<select value={selectedRecon} onChange={e=>setSelectedRecon(e.target.value)}><option value="">اختر كشفًا</option>{reconciliations.map(r=><option key={r.id} value={r.id}>{r.bank_name} · {r.statement_number} · {r.statement_date} {r.status==="reconciled"?"(معتمدة)":""}</option>)}</select></label></div>
      {summary&&<>
        <div className="treasury-summary-grid">
          <div><small>رصيد كشف البنك</small><strong>{money(summary.reconciliation.closing_balance)}</strong></div>
          <div><small>رصيد الدفاتر</small><strong>{money(summary.book_balance)}</strong></div>
          <div><small>حركات دفترية معلقة</small><strong>{money(summary.outstanding_total)}</strong></div>
          <div><small>الفرق غير المفسر</small><strong>{money(summary.unexplained_difference)}</strong></div>
        </div>
        {summary.reconciliation.status==="draft"&&<div className="treasury-csv-import">
          <div><strong>استيراد كشف البنك</strong><small>الصيغ: CSV · Excel XLSX · OFX/QFX · MT940. اختر الملف ثم راجع المعاينة قبل الحفظ.</small></div>
          <input type="file" accept=".csv,.txt,.xlsx,.ofx,.qfx,.mt940,.sta,.940,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={e=>{setStatementFile(e.target.files?.[0]||null);setStatementPreview(null);}}/>
          <button className="primary-button" type="button" disabled={busy||!statementFile} onClick={()=>void previewStatement()}>معاينة الملف</button>
          {statementPreview&&<div className="treasury-import-preview">
            <div className="treasury-summary-grid"><div><small>إجمالي الحركات</small><strong>{statementPreview.total}</strong></div><div><small>حركات جديدة</small><strong>{statementPreview.new_count}</strong></div><div><small>مكررات مستبعدة</small><strong>{statementPreview.duplicate_count}</strong></div></div>
            <small>الملف: {statementPreview.file_name} — لن يتم حفظ أي حركة قبل الضغط على تأكيد الاستيراد.</small>{statementPreview.rows_truncated&&<small>المعاينة تعرض أول 100 حركة جديدة فقط؛ سيتم فحص الملف كاملًا عند التأكيد.</small>}
            <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>المبلغ</th><th>البيان</th><th>المرجع</th></tr></thead><tbody>
              {statementPreview.rows.slice(0,30).map((row,index)=><tr key={index}><td>{row.date}</td><td>{money(row.amount)}</td><td>{row.description}</td><td>{row.reference}</td></tr>)}
              {!statementPreview.rows.length&&<tr><td colSpan={4}>لا توجد حركات جديدة؛ كل الحركات مكررة.</td></tr>}
            </tbody></table></div>
            {statementPreview.duplicates.length>0&&<details><summary>عرض الحركات المكررة المستبعدة ({statementPreview.duplicate_count})</summary><div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>المبلغ</th><th>البيان</th><th>المرجع</th></tr></thead><tbody>{statementPreview.duplicates.slice(0,30).map((row,index)=><tr key={index}><td>{row.date}</td><td>{money(row.amount)}</td><td>{row.description}</td><td>{row.reference}</td></tr>)}</tbody></table></div></details>}
            <button className="primary-button" type="button" disabled={busy||statementPreview.new_count===0} onClick={()=>void confirmStatementImport()}>تأكيد استيراد {statementPreview.new_count} حركة</button>
          </div>}
        </div>}
        {summary.reconciliation.status==="draft"&&<form className="treasury-form" onSubmit={addLine}>
          <label>رقم السطر<input required type="number" min="1" value={line.line_number} onChange={e=>setLine({...line,line_number:e.target.value})}/></label>
          <label>تاريخ الحركة<input required type="date" value={line.transaction_date} onChange={e=>setLine({...line,transaction_date:e.target.value})}/></label>
          <label>المبلغ (+ إيداع / - خصم)<input required type="number" step="0.0001" value={line.amount} onChange={e=>setLine({...line,amount:e.target.value})}/></label>
          <label>البيان<input value={line.description} onChange={e=>setLine({...line,description:e.target.value})}/></label>
          <label>المرجع<input value={line.reference} onChange={e=>setLine({...line,reference:e.target.value})}/></label>
          <div className="treasury-form-actions"><button className="primary-button" disabled={busy}>إضافة سطر كشف البنك</button></div>
        </form>}
        {summary.reconciliation.status==="draft"&&<div className="treasury-form"><h3 className="form-wide">تسجيل رسوم البنك أو الفوائد والحركات غير المسجلة</h3><label>الحساب المقابل<select value={adjustmentForm.counter_account} onChange={e=>setAdjustmentForm({...adjustmentForm,counter_account:e.target.value})}><option value="">اختر حساب مصروف/إيراد</option>{gl.filter(a=>a.is_active&&!a.is_group&&a.allow_posting).map(a=><option key={a.id} value={a.id}>{a.code} · {a.name_ar}</option>)}</select></label><label>السنة المالية<select value={adjustmentForm.fiscal_year} onChange={e=>setAdjustmentForm({...adjustmentForm,fiscal_year:e.target.value})}><option value="">اختر السنة المفتوحة</option>{years.filter(y=>y.status==="open").map(y=><option key={y.id} value={y.id}>{y.name}</option>)}</select></label>{!accounts.find(a=>a.id===summary.reconciliation.bank_account)?.branch&&<label>الفرع<select value={adjustmentForm.branch} onChange={e=>setAdjustmentForm({...adjustmentForm,branch:e.target.value})}><option value="">اختر الفرع</option>{branches.map(b=><option key={b.id} value={b.id}>{b.name_ar}</option>)}</select></label>}<p className="form-wide">المبلغ الموجب ينشئ سند قبض (مثل فوائد دائنة)، والسالب ينشئ سند صرف (مثل عمولات البنك). ينشأ السند كمسودة ويخضع لمسار الاعتماد والترحيل المعتاد.</p></div>}
        <div className="data-table-wrap"><table><thead><tr><th>التاريخ</th><th>البيان</th><th>المرجع</th><th>المبلغ</th><th>المطابق</th><th>المتبقي</th><th>الحركة الدفترية</th><th>مبلغ التخصيص</th><th>إجراء</th></tr></thead><tbody>{summary.lines.map(row=>{const candidates=eligible(row.amount);const selected=candidates.find(t=>String(t.id)===matchIds[row.id]);return <tr key={row.id}><td>{row.transaction_date}</td><td>{row.description}</td><td>{row.reference}</td><td>{money(row.amount)}</td><td>{row.matched_document||"غير مطابق"}{row.allocations.length>0&&<small> · {row.allocations.length} تخصيص</small>}{row.adjustment_document&&!row.matched_transaction?<small> · سند {row.adjustment_document} ({row.adjustment_status==="posted"?"مرحّل":"مسودة"})</small>:null}</td><td>{money(row.remaining_amount)}</td><td>{Number(row.remaining_amount)>0&&summary.reconciliation.status==="draft"?<select value={matchIds[row.id]||""} onChange={e=>{const id=e.target.value;setMatchIds({...matchIds,[row.id]:id});const chosen=candidates.find(t=>String(t.id)===id);if(chosen)setMatchAmounts({...matchAmounts,[row.id]:Math.min(Number(row.remaining_amount),Number(chosen.remaining_amount)).toFixed(4)});}}><option value="">اختر حركة دفترية</option>{candidates.map(t=><option key={t.id} value={t.id}>{t.document_number} · {t.date} · متبقي {money(t.remaining_amount)}</option>)}</select>:row.matched_document}</td><td>{Number(row.remaining_amount)>0&&summary.reconciliation.status==="draft"?<input type="number" min="0.0001" max={row.remaining_amount} step="0.0001" value={matchAmounts[row.id]||""} onChange={e=>setMatchAmounts({...matchAmounts,[row.id]:e.target.value})} aria-label="مبلغ التخصيص"/>:"—"}</td><td>{summary.reconciliation.status==="draft"?<>{row.allocations.length>0&&<button className="subtle-button" disabled={busy} onClick={()=>void unmatch(row.id)}>فك التخصيصات</button>}{!row.adjustment_transaction&&Number(row.remaining_amount)>0&&<button className="subtle-button" disabled={busy||!adjustmentForm.counter_account||!adjustmentForm.fiscal_year} onClick={()=>void createAdjustment(row.id)}>إنشاء سند مقابل</button>}<button className="subtle-button" disabled={busy||!matchIds[row.id]||!matchAmounts[row.id]} onClick={()=>void match(row.id)}>تخصيص المبلغ</button></>:"—"}</td></tr>;})}</tbody></table></div>
        <div className="treasury-recon-footer"><span>مطابق: {summary.matched_lines} · غير مطابق: {summary.unmatched_lines} · حركات دفترية معلقة: {summary.outstanding_transactions.length}</span>{summary.reconciliation.status==="draft"?<button className="primary-button" disabled={busy||summary.unmatched_lines>0||Number(summary.statement_difference)!==0||Number(summary.unexplained_difference)!==0} onClick={()=>void finalize()}>اعتماد التسوية</button>:<strong>تم اعتماد التسوية بتاريخ {summary.reconciliation.reconciled_at?.slice(0,10)}</strong>}</div>
        {summary.reconciliation.status==="reconciled"&&<div className="treasury-form"><label>سبب إعادة فتح التسوية<textarea required minLength={10} maxLength={500} value={reopenReason} onChange={e=>setReopenReason(e.target.value)} placeholder="اكتب سبب التصحيح أو المراجعة"/></label><button type="button" className="subtle-button" disabled={busy||reopenReason.trim().length<10} onClick={()=>void reopen()}>إعادة فتح التسوية</button><small>لا يمكن فتح تسوية قديمة إذا وُجدت تسوية لاحقة معتمدة على الحساب نفسه؛ يجب البدء من الأحدث.</small></div>}
        {summary.outstanding_transactions.length>0&&<div className="data-table-wrap"><h3>حركات دفترية معلقة / فروق توقيت</h3><table><thead><tr><th>التاريخ</th><th>المستند</th><th>البيان</th><th>المتبقي</th><th>فرق التوقيت بالأيام</th></tr></thead><tbody>{summary.outstanding_transactions.map(t=><tr key={t.id}><td>{t.date}</td><td>{t.document_number}</td><td>{t.description}</td><td>{money(t.amount)}</td><td>{t.timing_difference_days}</td></tr>)}</tbody></table></div>}
      </>}
    </>}
  </section>;
}
