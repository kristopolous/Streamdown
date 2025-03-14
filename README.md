# Streamdown

I needed a streaming Markdown TUI CLI shell parser and honestly all the ones I found sucked. They were broken or janky in some kind of way. So here we go:

[simplescreenrecorder-2025-03-12_17.58.07.webm](https://github.com/user-attachments/assets/de4860d5-dd0e-411f-bda3-e3d60deb7938)

This will work with [swillison's llm](https://github.com/simonw/llm) unlike with [richify.py](https://github.com/gianlucatruda/richify) which jumps around the page or blocks with an elipses or [glow](https://github.com/charmbracelet/glow) which buffers everything, this streams and does exactly what you want.

## Some Features

#### Provides wrap-around in code for long code blocks and short terminals. It just has a block of a different background to signify it so if you copy and paste all it does is inject whitespace ... not terrible, not terrible

  
![wraparound](https://github.com/user-attachments/assets/8a4319a3-1182-4dba-92ce-7b240d3c7dec)


#### Does OSC 8 links for modern terminals.

[links.webm](https://github.com/user-attachments/assets/a5f71791-7c58-4183-ad3b-309f470c08a3)


#### Doesn't consume characters like _ and * as style when they are in `blocks like this` because `_they_can_be_varaiables_`
![dunder](https://github.com/user-attachments/assets/5425caf0-67be-4e8e-b5d4-765913fd54aa)

## Demo
Do this

    $ ./tester.sh tests/*md | ./sd.py

Certainly room for improvement and I'll probably continue to make them

 * tables don't currently stream. it's actually a sophisticated problem. i've got a solution but I want to just have the llm do it without having to think about it. I am theoretically not always this lazy.

* ingest the first 2 rows. compute the division from there and do wrap on the cells

* alternatively do equal width and permit sub optimal widths.

* lastly, this is inspired from sqlite, we can do key/value rows as individual tables, which changes the layout but makes large row tables not cascade down the screen in some wraparound mess.
