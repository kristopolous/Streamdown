# Streamdown

I needed a streaming Markdown TUI CLI shell parser and honestly all the ones I found lacking. They were broken or janky in some kind of way. So here we go. From the ground up. It's a bad idea but it has to be done.

[sd demo](https://github.com/user-attachments/assets/48dba6fa-2282-4be9-8087-a2ad8e7c7d12)


This will work with [simonw's llm](https://github.com/simonw/llm) unlike with [richify.py](https://github.com/gianlucatruda/richify) which jumps around the page or blocks with an elipses or [glow](https://github.com/charmbracelet/glow) which buffers everything, this streams and does exactly what you want.

## Some Features

#### Provides clean copyable code for long code blocks and short terminals. 
![copyable](https://github.com/user-attachments/assets/7462c278-904c-4dbc-b09d-72254e7e639d)

#### Does OSC 8 links for modern terminals.

[links.webm](https://github.com/user-attachments/assets/a5f71791-7c58-4183-ad3b-309f470c08a3)


#### Doesn't consume characters like _ and * as style when they are in `blocks like this` because `_they_can_be_varaiables_`
![dunder](https://github.com/user-attachments/assets/eb9ab001-3bc7-4e4b-978f-bc00f29c2a41)

## Demo
Do this

    $ ./tester.sh tests/*md | ./sd.py

Certainly room for improvement and I'll probably continue to make them

 * tables don't currently stream. it's actually a sophisticated problem. i've got a solution but I want to just have the llm do it without having to think about it. I am theoretically not always this lazy.

* ingest the first 2 rows. compute the division from there and do wrap on the cells

* alternatively do equal width and permit sub optimal widths.

* lastly, this is inspired from sqlite, we can do key/value rows as individual tables, which changes the layout but makes large row tables not cascade down the screen in some wraparound mess.
