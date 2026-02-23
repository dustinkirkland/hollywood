# hollywood

Fill your console with Hollywood melodrama technobabble.

This utility splits your terminal into multiple panes of genuine technobabble, perfectly suitable for any Hollywood geek melodrama.  It is particularly suitable on any number of computer consoles in the background of any excellent schlock technothriller.

For more information, visit [hollywood.computer](https://a.hollywood.computer/).

## Quick Start

Run in Docker (no installation needed):

```bash
docker run -it --rm cgr.dev/chainguard/hollywood
```

Install on Ubuntu/Debian:

```bash
sudo apt install hollywood
```

Install on [Chainguard OS](https://www.chainguard.dev/chainguard-os) (Wolfi):

```bash
apk add hollywood
```

## Usage

```bash
hollywood                          # run with defaults
hollywood -s 5                     # limit to 5 splits
hollywood -d 30                    # rearrange panes every 30 seconds
hollywood -s 8 -d 15              # combine options
```

Press `Ctrl-C` to quit.

## Samples

- [YouTube stream](https://www.youtube.com/watch?v=18RSYZa58FE)
- [MP4 download](https://drive.google.com/uc?id=1PK4IKYw_5R9D4BQEepmNmCbiWqSq4nks&export=download) (suitable as a Zoom/Meet background)

## As Seen On

Hollywood has been spotted in the wild on television, in commercials, and across the internet:

- [NBC News](https://www.nbcnews.com/tech/security/americans-are-getting-freaked-out-about-doing-stuff-internet-n574661)
- [CNBC](https://www.cnbc.com/video/2020/06/17/cnbcs-disruptor-50-attabotics.html)
- [Saturday Night Live](https://www.nbc.com/saturday-night-live/video/mr-robot/3108917)
- [Netflix "Unit 42"](https://www.netflix.com/title/81027195)
- [Yahoo Finance](https://finance.yahoo.com/news/mother-data-breaches-26-billion-173012707.html)
- [Full Frontal with Samantha Bee](https://www.youtube.com/watch?v=C8AxAvh3-ck&t=54)
- [Map Men](https://www.youtube.com/watch?v=yTyX_EJQOIU&t=323)
- [Spy Ninjas](https://www.youtube.com/watch?v=JM9u9ehNscI&ab_channel=PROJECTZORGO-SPYNINJAHacker&t=43)
- [Experian](https://www.youtube.com/watch?v=vBEKs_Priu0)
- [SentinelOne](https://twitter.com/DustinKirkland/status/1303879028886175745)
- [SUSE parody music video](https://www.youtube.com/watch?v=b0tsZB_LEQk)
- DEFCON music channel
- [Texas A&M Foundation magazine](https://www.txamfoundation.com/Spring-2019/Cover-Feature.aspx)

## Widgets

Hollywood includes 20 visual widgets that are randomly selected and arranged:

| Widget | Description |
|--------|-------------|
| `apg` | Random password generation |
| `atop` | Advanced system monitor |
| `bat` | Syntax-highlighted source code |
| `bmon` | Network bandwidth monitor |
| `cmatrix` | Matrix-style digital rain |
| `code` | Colorized source code display |
| `errno` | Scrolling error code definitions |
| `figlet` | Large ASCII text banners |
| `hexdump` | Hex dump of binary files |
| `htop` | Interactive process viewer |
| `jp2a` | JPEG to ASCII art conversion |
| `logs` | Colorized system log files |
| `man` | Random colorized man pages |
| `map` | World map in ASCII art |
| `mplayer` | Video to ASCII art animation |
| `pv` | Data transfer progress bars |
| `speedometer` | Network traffic graphs |
| `sshart` | SSH key fingerprint art |
| `stat` | File and device statistics |
| `tree` | Directory tree visualization |

## Security Note

Some widgets display real system information (processes, logs, network stats, file system details) that could be sensitive.  For demonstrations or presentations, consider running in a container:

```bash
docker run -it --rm dustinkirkland/hollywood
```

## Contributing

Widget design rules:

- Must work as a non-root user
- Must display information indefinitely
- Must not exhaust memory or overload the system
- Must not be too egregious with I/O
- Must not require outbound internet access

## Packages

- [Source code](https://github.com/dustinkirkland/hollywood)
- [Ubuntu](http://pad.lv/u/hollywood)
- [Debian](https://packages.qa.debian.org/h/hollywood.html)

## License

Apache-2.0.  See the [LICENSE](LICENSE) file or [apache.org/licenses/LICENSE-2.0](https://www.apache.org/licenses/LICENSE-2.0).
