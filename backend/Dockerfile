FROM ghcr.io/steamdeckhomebrew/holo-base:latest
 
RUN mkdir /pacman && pacman -Sydd --noconfirm --root /pacman --dbpath /var/lib/pacman gstreamer-vaapi gst-plugin-pipewire gst-plugins-bad gst-plugins-bad-libs gst-plugins-good gst-libav

RUN cd /pacman/usr/lib/gstreamer-1.0/ && rm libgstneonhttpsrc.so libgstfaad.so libgstcacasink.so libgstaasink.so libgstspandsp.so libgstgme.so libgstteletext.so libgstshout2.so libgstchromaprint.so libgstkate.so libgstwildmidi.so libgstmusepack.so libgstmplex.so libgsttimecode.so libgstavtp.so libgstwebrtc.so libgsttwolame.so libgstmpeg2enc.so libgstopenmpt.so libgstdtsdec.so libgstzbar.so libgstresindvd.so libgstdc1394.so libgstsoundtouch.so libgstmicrodns.so libgstmpg123.so libgstrtmp.so libgstopenexr.so libgstfluidsynthmidi.so libgstsvthevcenc.so libgstwavpack.so libgstde265.so libgstsrtp.so libgstladspa.so libgstdv.so

RUN pacman -Sydd --noconfirm --dbpath /var/lib/pacman python-pip

RUN pip3 install psutil --target=/psutil

ENTRYPOINT [ "/backend/entrypoint.sh" ]