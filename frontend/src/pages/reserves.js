        import {workspace} from '../workspace.js';
        import {el} from '../dom.js';

        const page = {
  "title": "从两日用量核算备用",
  "module": "备用分钟核算",
  "caption": "备用分钟核算",
  "detail": "以 24 小时周期的 Fourier 基拟合，并保留末六小时验证。",
  "path": "/reserves",
  "columns": [
    {
      "name": "code",
      "label": "编号"
    },
    {
      "name": "airslot",
      "label": "节目时段"
    },
    {
      "name": "plan_wh",
      "label": "计划备用 Wh"
    },
    {
      "name": "fallback",
      "label": "备用源优先级"
    },
    {
      "name": "mae",
      "label": "时间验证MAE Wh"
    },
    {
      "name": "token",
      "label": "计算号"
    }
  ],
  "layout": "split",
  "view": "grid",
  "actions": [
    {
      "name": "forecast_wh",
      "label": "计算周期用量与备量",
      "path": "/reserves/forecast-wh",
      "fields": [
        {
          "name": "airslot",
          "label": "节目编号",
          "kind": "text",
          "initial": "AIR-17"
        },
        {
          "name": "meter",
          "label": "电表",
          "kind": "text",
          "initial": "BAT-04"
        },
        {
          "name": "clear",
          "label": "计划日晴天",
          "kind": "bool",
          "initial": true
        }
      ],
      "help": "使用整点记录和次日晴天状态，返回备用建议。",
      "roles": [
        "operator",
        "reviewer"
      ],
      "success": "已写入"
    },
    {
      "name": "save_reserve",
      "label": "记录备用量评估",
      "path": "/reserves/save-reserve",
      "fields": [
        {
          "name": "code",
          "label": "备用评估单",
          "kind": "text",
          "initial": "RV-24"
        },
        {
          "name": "token",
          "label": "计算号",
          "kind": "token",
          "initial": ""
        }
      ],
      "help": "按调班时对应的许可情况留下备用评估。已备案的计算号重试会返回原单，不新增评估。",
      "roles": [
        "operator",
        "reviewer"
      ],
      "success": "已写入"
    },
    {
      "name": "lookup_compute",
      "label": "按计算号查询任务",
      "kind": "lookup",
      "fields": [
        {
          "name": "token",
          "label": "计算号",
          "kind": "token",
          "initial": ""
        }
      ],
      "help": "查看计算号当前状态；任务被接手后会显示接手轮次，算完即可备案。",
      "roles": [
        "operator",
        "reviewer",
        "viewer"
      ],
      "success": "已查询"
    },
    {
      "name": "takeover_compute",
      "label": "中断接手续跑",
      "kind": "takeover",
      "fields": [
        {
          "name": "token",
          "label": "计算号",
          "kind": "token",
          "initial": ""
        }
      ],
      "help": "原机器中断时接手：沿用原计算号和已审签读数，接手后原机器交回的结果不再采用。",
      "roles": [
        "operator",
        "reviewer"
      ],
      "success": "已接手"
    }
  ]
};

        export const {title} = page;
        export const key = "reserves";
        export async function render(root, role) {
          await workspace(root, role, page);

        }
