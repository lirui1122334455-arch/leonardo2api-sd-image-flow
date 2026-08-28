# Fish Audio S2.1-Pro 情绪标签中文对照

更新时间：2026-08-07

## 1. 使用说明

当前项目的 Fish Audio 任务使用 `s2.1-pro`，情绪和语气通过 `text` 字段直接透传，
不需要额外的 `emotion` 参数。

S2.1-Pro 使用方括号标签：

```text
[happy] 今天真是美好的一天！
[sad][whispering] 对不起，我可能要离开了。
```

前端可以显示中文名称，但提交请求时建议使用本表中的英文标签。例如用户选择“兴奋”时，
在对应句子前加入 `[excited]`。

```json
{
  "task_type_code": "fish_audio_workflow",
  "json": {
    "text": "[excited] 太好了，我们成功了！",
    "reference_id": "59cb5986671546eaa6ca8ae6f29f6d22",
    "backend": "s2.1-pro"
  }
}
```

## 2. 基础情绪

| 英文标签 | 中文名称 | 适用场景 | 完整示例 |
|---|---|---|---|
| `[happy]` | 开心、愉快 | 好消息、问候 | `[happy] 太好了，今天真是美好的一天！` |
| `[sad]` | 悲伤、低落 | 安慰、坏消息 | `[sad] 听到这个消息，我真的很难过。` |
| `[angry]` | 愤怒 | 抱怨、警告 | `[angry] 你怎么能一再违背我们的约定！` |
| `[excited]` | 兴奋、激动 | 宣布消息、庆祝 | `[excited] 我们终于赢得比赛了！` |
| `[calm]` | 平静、沉稳 | 说明、冥想 | `[calm] 请慢慢呼吸，让身体放松下来。` |
| `[nervous]` | 紧张、不安 | 道歉、免责声明 | `[nervous] 我不确定自己是否准备好了。` |
| `[confident]` | 自信、坚定 | 演示、销售 | `[confident] 我们一定能按时完成这个项目。` |
| `[surprised]` | 惊讶、意外 | 反应、发现 | `[surprised] 什么？你已经把问题解决了？` |
| `[satisfied]` | 满意、满足 | 确认、评价 | `[satisfied] 这个结果正是我想要的。` |
| `[delighted]` | 欣喜、非常高兴 | 庆祝、赞美 | `[delighted] 能再次见到你，我实在太高兴了！` |
| `[scared]` | 害怕、恐惧 | 警告、恐怖故事 | `[scared] 门外好像有人，我们该怎么办？` |
| `[worried]` | 担忧 | 表达顾虑、提问 | `[worried] 他这么久没有回复，不会出事吧？` |
| `[upset]` | 难过、心烦 | 抱怨、问题反馈 | `[upset] 我没想到你会这样对我。` |
| `[frustrated]` | 懊恼、受挫 | 故障、延误 | `[frustrated] 我已经试了五次，怎么还是不行？` |
| `[depressed]` | 消沉、绝望 | 严肃或沉重内容 | `[depressed] 我感觉一切都失去了意义。` |
| `[empathetic]` | 共情、体谅 | 客服、安慰、咨询 | `[empathetic] 我理解你现在一定很不好受。` |
| `[embarrassed]` | 尴尬 | 道歉、犯错 | `[embarrassed] 抱歉，我刚才叫错了你的名字。` |
| `[disgusted]` | 厌恶 | 负面评价 | `[disgusted] 这里的气味实在让人受不了。` |
| `[moved]` | 感动 | 温情时刻 | `[moved] 谢谢你一直记得我们的约定。` |
| `[proud]` | 自豪 | 成就、表扬 | `[proud] 你完成得非常出色，我为你自豪。` |
| `[relaxed]` | 放松、随意 | 日常对话 | `[relaxed] 今天没什么安排，我们慢慢来。` |
| `[grateful]` | 感激 | 感谢、致谢 | `[grateful] 谢谢你在我最困难的时候帮助我。` |
| `[curious]` | 好奇 | 提问、探索 | `[curious] 这个装置到底是怎么工作的？` |
| `[sarcastic]` | 讽刺、嘲讽 | 幽默、批评 | `[sarcastic] 是啊，你迟到两个小时可真准时。` |

## 3. 高级情绪

| 英文标签 | 中文名称 | 适用场景 | 完整示例 |
|---|---|---|---|
| `[disdainful]` | 鄙夷、不屑 | 批评、拒绝 | `[disdainful] 这种毫无根据的说法，根本不值得回应。` |
| `[unhappy]` | 不开心、不满 | 投诉、反馈 | `[unhappy] 这次服务体验没有达到我的预期。` |
| `[anxious]` | 焦虑 | 紧急事项 | `[anxious] 时间不多了，我们还没有收到确认。` |
| `[hysterical]` | 歇斯底里、情绪失控 | 极端情绪反应 | `[hysterical] 不！这不可能！你们都在骗我！` |
| `[indifferent]` | 冷漠、无所谓 | 冷淡或中性回应 | `[indifferent] 随便吧，选哪个对我都一样。` |
| `[uncertain]` | 迟疑、不确定 | 推测、提问 | `[uncertain] 也许这样可行，但我还不能确定。` |
| `[doubtful]` | 怀疑 | 质疑、不相信 | `[doubtful] 你确定这份数据真的可靠吗？` |
| `[confused]` | 困惑 | 请求解释 | `[confused] 等一下，这两个步骤不是互相矛盾吗？` |
| `[disappointed]` | 失望 | 期望落空 | `[disappointed] 我原本以为你会遵守承诺。` |
| `[regretful]` | 后悔、懊悔 | 道歉、犯错 | `[regretful] 如果当时听你的建议就好了。` |
| `[guilty]` | 内疚 | 坦白、道歉 | `[guilty] 是我弄丢了文件，对不起。` |
| `[ashamed]` | 羞愧 | 严重失误 | `[ashamed] 我不该为了掩饰错误而撒谎。` |
| `[jealous]` | 嫉妒 | 比较、情感冲突 | `[jealous] 为什么所有人都只关注他？` |
| `[envious]` | 羡慕、嫉羡 | 带渴望的羡慕 | `[envious] 我真羡慕你能做自己喜欢的工作。` |
| `[hopeful]` | 充满希望 | 未来计划 | `[hopeful] 只要继续努力，明天一定会更好。` |
| `[optimistic]` | 乐观 | 鼓励、积极展望 | `[optimistic] 这只是暂时的困难，我们很快会找到办法。` |
| `[pessimistic]` | 悲观 | 警示、疑虑 | `[pessimistic] 照这个趋势，恐怕不会有好结果。` |
| `[nostalgic]` | 怀旧 | 回忆、故事 | `[nostalgic] 小时候每到夏天，我们都会在河边玩。` |
| `[lonely]` | 孤独 | 情感内容 | `[lonely] 房间很安静，已经很久没有人来找我了。` |
| `[bored]` | 无聊、厌倦 | 表达无兴趣 | `[bored] 这个话题我们已经重复讨论很多遍了。` |
| `[contemptuous]` | 轻蔑 | 强烈批评 | `[contemptuous] 连基本事实都不愿核实，这种态度太可笑了。` |
| `[sympathetic]` | 同情 | 慰问、哀悼 | `[sympathetic] 听说你失去了亲人，我很遗憾。` |
| `[compassionate]` | 怜悯、关怀 | 支持、帮助 | `[compassionate] 你不用独自承受，我们会陪着你。` |
| `[determined]` | 坚定、坚决 | 目标、承诺 | `[determined] 无论遇到什么困难，我都会坚持到底。` |
| `[resigned]` | 无奈接受、认命 | 放弃、接受结果 | `[resigned] 好吧，既然无法改变，那就接受现实吧。` |

## 4. 语气与说话方式

以下标签不是情绪，用于控制音量、力度和表达方式。

| 英文标签 | 中文名称 | 使用说明 | 完整示例 |
|---|---|---|---|
| `[in a hurry tone]` | 急促、赶时间 | 表达紧迫信息 | `[in a hurry tone] 快点，我们马上就要迟到了！` |
| `[shouting]` | 大声喊叫 | 引起注意 | `[shouting] 大家快离开这里！` |
| `[screaming]` | 尖叫、惊恐大喊 | 紧急、恐惧场景 | `[screaming] 小心！后面有车！` |
| `[whispering]` | 低声耳语 | 秘密、安静场景 | `[whispering] 别出声，他就在门外。` |
| `[soft tone]` | 轻柔、温和 | 安慰、摇篮曲 | `[soft tone] 别担心，一切都会好起来的。` |
| `[emphasis]` | 强调 | 放在需要强调的词语或短语前 | `这件事情[emphasis]非常重要。` |

强调示例：

```text
这件事情[emphasis]非常重要。
```

## 5. 人声效果

| 英文标签 | 中文名称 | 完整示例 |
|---|---|---|
| `[laughing]` | 大笑 | `[laughing] 哈哈哈，这也太有意思了！` |
| `[chuckling]` | 轻笑、窃笑 | `[chuckling] 呵呵，我早就猜到了。` |
| `[sobbing]` | 啜泣 | `[sobbing] 我真的不想和你告别。` |
| `[crying loudly]` | 放声大哭 | `[crying loudly] 为什么事情会变成这样！` |
| `[sighing]` | 叹气 | `[sighing] 唉，看来只能重新开始了。` |
| `[groaning]` | 呻吟、抱怨声 | `[groaning] 唉，这工作什么时候才能做完。` |
| `[panting]` | 喘气 | `[panting] 等等我，我快跑不动了。` |
| `[gasping]` | 倒吸一口气 | `[gasping] 天啊，这是真的吗？` |
| `[yawning]` | 打哈欠 | `[yawning] 好困啊，我该去睡觉了。` |
| `[snoring]` | 打鼾 | `[snoring] 呼噜，呼噜。` |
| `[clear throat]` | 清嗓子 | `[clear throat] 咳咳，请大家安静一下。` |

## 6. 环境与停顿效果

| 英文标签 | 中文名称 | 完整示例 |
|---|---|---|
| `[audience laughing]` | 观众笑声 | `他说完最后一句笑话。[audience laughing]` |
| `[background laughter]` | 背景笑声 | `聚会还在热闹地进行。[background laughter]` |
| `[crowd laughing]` | 人群笑声 | `台上的意外动作逗乐了所有人。[crowd laughing]` |
| `[break]` | 短暂停顿 | `接下来公布结果。[break] 第一名是小李。` |
| `[long-break]` | 较长停顿 | `我想告诉你一件事。[long-break] 我要离开这里了。` |

## 7. 强度和自由描述

S2.1-Pro 支持方括号内的自然语言描述，不限于固定标签。为了保证生成结果可控，
常用程度修饰建议如下：

| 标签示例 | 中文含义 |
|---|---|
| `[slightly sad]` | 略微悲伤 |
| `[very excited]` | 非常兴奋 |
| `[extremely angry]` | 极度愤怒 |
| `[warm and happy]` | 温暖而开心 |

固定标签更适合前端下拉选项；自由描述更适合高级输入框。自由描述的实际表现会受到音色、
文本内容和上下文影响。

## 8. 组合示例

```text
[sad][whispering] 我真的很想念你。
[angry][shouting] 马上停下来！
[excited][laughing] 我们成功了，哈哈！
[nervous][uncertain] 你确定这样没问题吗？
```

情绪过渡示例：

```text
[happy] 我得到晋升了！
[uncertain] 但是，这意味着我要搬到另一个城市。
[sad] 我会很想念大家。
[hopeful] 不过，这也是一个很好的机会。
[determined] 我一定会做好这份工作！
```

## 9. 使用规则

1. 句子级情绪标签建议放在句首。
2. 每句话建议只使用一个主要情绪。
3. 一句话最多组合三个标签。
4. 不要在短文本中堆叠大量标签，也不要混合互相冲突的情绪。
5. `[emphasis]` 应紧邻需要强调的词语。
6. 笑声、叹气等效果可以配合“哈哈”“唉”等自然文字，提高稳定性。
7. 不同音色的情绪表现力度不同，上线前应按常用音色逐一试听。
8. S2.1-Pro 使用 `[标签]`；旧版 S1 使用 `(标签)`，二者不要混用。

## 10. 前端选项建议

前端选项可以采用以下数据结构：

```json
{
  "label": "兴奋",
  "value": "excited",
  "marker": "[excited]",
  "category": "basic_emotion"
}
```

生成请求时，将 `marker` 插入对应句子的开头，再通过现有 `text` 字段提交。后端无需增加新的
Fish Audio 请求参数。

## 11. 官方资料

- Fish Audio Emotion Control：<https://docs.fish.audio/developer-guide/core-features/emotions>
- Fish Audio Capabilities：<https://docs.fish.audio/overview/capabilities>
