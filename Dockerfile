FROM hdlc/yosys

RUN apt-get update && apt-get install -y python3 python3-pip && \
    pip3 install flask flask-cors --break-system-packages && \
    rm -rf /var/lib/apt/lists/*

COPY server.py /app/server.py
WORKDIR /app
EXPOSE 10000
CMD ["python3", "server.py"]