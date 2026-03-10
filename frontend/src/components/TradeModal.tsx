import { X, Cpu, Clock, Target, Info, Scale } from 'lucide-react';
import type { DisplayTrade } from '../types';

interface Props {
    trade: DisplayTrade;
    onClose: () => void;
}

export default function TradeModal({ trade, onClose }: Props) {
    const isWin = trade.profit >= 0;

    return (
        <div className="fixed inset-0 z-[120] flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm overflow-y-auto">
            <div className="bg-white w-full max-w-2xl rounded-2xl shadow-2xl overflow-hidden border border-slate-200">
                {/* Header */}
                <div className={`p-6 border-b border-slate-100 flex justify-between items-start ${isWin ? 'bg-emerald-50' : 'bg-rose-50'}`}>
                    <div>
                        <div className="flex items-center gap-3 mb-1">
                            <h3 className="text-2xl font-black text-slate-900">{trade.stock}</h3>
                            <span className={`px-2.5 py-0.5 rounded-full text-xs font-black tracking-widest uppercase ${
                                trade.type === 'Long'
                                    ? 'bg-emerald-500 text-white'
                                    : 'bg-rose-500 text-white'
                            }`}>
                                {trade.type}
                            </span>
                        </div>
                        <div className="flex items-center gap-4 text-slate-500 text-xs">
                            <span className="flex items-center gap-1"><Cpu size={12} /> ID: {trade.id}</span>
                            <span className="flex items-center gap-1"><Clock size={12} /> {trade.duration}</span>
                        </div>
                    </div>
                    <button onClick={onClose} className="p-2 hover:bg-white/70 rounded-xl text-slate-400 hover:text-slate-700 transition-all">
                        <X size={20} />
                    </button>
                </div>

                {/* Body */}
                <div className="p-6 space-y-6">
                    <div className="flex flex-col items-center justify-center py-4">
                        <p className="text-[10px] text-slate-400 uppercase font-black tracking-[0.2em] mb-2">Realized P/L</p>
                        <p className={`text-6xl font-mono font-black tracking-tighter ${isWin ? 'text-emerald-600' : 'text-rose-500'}`}>
                            ${trade.profit.toFixed(2)}
                        </p>
                        <div className="mt-3 flex items-center gap-3">
                            <span className="text-xs font-semibold text-slate-500 bg-slate-100 px-3 py-1 rounded-full border border-slate-200">
                                {trade.reason}
                            </span>
                            <span className="text-xs font-bold text-slate-400 uppercase">
                                R: {trade.pl_R?.toFixed(2)}
                            </span>
                        </div>
                    </div>

                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 bg-slate-50 p-5 rounded-xl border border-slate-100">
                        <div className="space-y-1">
                            <p className="text-[9px] text-slate-400 uppercase font-bold flex items-center gap-1"><Target size={11} /> Entry</p>
                            <p className="text-base font-mono font-bold text-slate-800">${trade.entry_price.toFixed(2)}</p>
                        </div>
                        <div className="space-y-1">
                            <p className="text-[9px] text-slate-400 uppercase font-bold flex items-center gap-1"><Info size={11} /> Exit</p>
                            <p className="text-base font-mono font-bold text-slate-800">${trade.exit_price.toFixed(2)}</p>
                        </div>
                        <div className="space-y-1">
                            <p className="text-[9px] text-slate-400 uppercase font-bold flex items-center gap-1"><Clock size={11} /> Duration</p>
                            <p className="text-base font-mono font-bold text-slate-800">{trade.duration}</p>
                        </div>
                        <div className="space-y-1">
                            <p className="text-[9px] text-slate-400 uppercase font-bold flex items-center gap-1"><Scale size={11} /> Size</p>
                            <p className="text-base font-mono font-bold text-slate-800">{trade.size}</p>
                        </div>
                    </div>
                </div>

                {/* Footer */}
                <div className="p-6 border-t border-slate-100">
                    <button
                        onClick={onClose}
                        className="w-full bg-slate-100 hover:bg-slate-200 text-slate-600 font-bold py-3 rounded-xl transition-all uppercase tracking-widest text-xs"
                    >
                        Close
                    </button>
                </div>
            </div>
        </div>
    );
}
