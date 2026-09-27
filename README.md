# 实验预习答题助手

某高校「实验预习答题系统」（内网）的自动答题工具。自动答题、自动交卷，
没拿满分就自己找出错题改掉重交，直到满分。

> ⚠️ 仅供学习交流与个人核对答案使用。请自行确认这种做法是否符合你所在课程与学校的规定，作者不对使用后果负责。

---

## 怎么用

### 方式一：免安装 exe（推荐，啥都不用装）

1. 到 [Releases](https://github.com/FocuZHe/lab-preview-quiz-auto/releases) 下载
   `lab-quiz-assistant-v1.0.0.exe`（约 42 MB）；
2. 放进任意**能写文件**的文件夹（别放 `C:\Program Files`）；
3. 双击打开，输入学号、密码，回车。

就这些，剩下的它自己干。跑完会在 exe 旁边生成 `阅卷PDF/` 文件夹和 `bank.json`。

> 附件用英文名是因为 GitHub 会把非 ASCII 的附件名吃掉。下下来随便改成什么名字都行。

### 方式二：Python 脚本

```bash
pip install pdfplumber

python lab_quiz.py -u 学号 -p 密码 summary   # 先看看有哪些卷子、各得多少分
python lab_quiz.py -u 学号 -p 密码 solve     # 全部做完
```

Windows 上也可以直接双击 `一键答题.bat`，效果和 exe 一样。

缺 `pdfplumber` 时脚本会**自动 pip 安装**。想关掉自动安装，设环境变量 `LABQUIZ_NO_INSTALL=1`。

`solve` 是幂等的，反复跑不会出问题；已经满分的卷子不会被重做。

---

## 跑完能得到什么

- 每份实验预习**全部满分并自动提交**；
- 同目录下 `阅卷PDF/` 里是各份卷子的阅卷 PDF，文件名形如
  `01_弗兰克-赫兹实验_100分.pdf`。

---

## 常用参数

| 参数 | 说明 |
|---|---|
| `--id 3,5,9` | 只处理指定卷号 |
| `--dry-run` | 只算答案、不交卷 |
| `--force` | 已满分的也重做 |
| `--only-undone` | 跳过已标记完成的 |
| `--account 学号:密码` | 指定账号，可重复写多个批量跑 |
| `--base <url>` | 换接口地址（默认 `http://172.25.75.220/api`） |

其余开关和子命令（`build-bank` / `verify-bank` / `parse` 等）见：

```bash
python lab_quiz.py --help
```

---

## 自己打包 exe

双击 `build_exe.bat` 即可（需要先装好 Python，以及 `pip install pyinstaller pdfplumber`）。

---

## License

MIT
