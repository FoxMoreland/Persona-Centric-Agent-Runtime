package io.github.foxmoreland.pcar.runtimecheck;

import ai.onnxruntime.OnnxTensor;
import ai.onnxruntime.OrtEnvironment;
import ai.onnxruntime.OrtSession;

import java.util.Map;

final class BgeSession {
    private final OrtEnvironment environment = OrtEnvironment.getEnvironment();
    private final OrtSession session;

    BgeSession(String modelPath) throws Exception {
        try (OrtSession.SessionOptions options = new OrtSession.SessionOptions()) {
            options.setIntraOpNumThreads(2);
            options.setInterOpNumThreads(1);
            options.setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT);
            session = environment.createSession(modelPath, options);
        }
    }

    synchronized float[] encode(int[] tokens) throws Exception {
        if (tokens.length == 0 || tokens.length > 512) {
            throw new IllegalArgumentException("Unexpected BGE input length");
        }
        long[][] ids = new long[1][tokens.length];
        long[][] attention = new long[1][tokens.length];
        for (int i = 0; i < tokens.length; i++) {
            ids[0][i] = tokens[i];
            attention[0][i] = 1;
        }
        try (OnnxTensor input = OnnxTensor.createTensor(environment, ids);
             OnnxTensor mask = OnnxTensor.createTensor(environment, attention);
             OnnxTensor types = OnnxTensor.createTensor(environment, new long[1][tokens.length]);
             OrtSession.Result output = session.run(Map.of("input_ids", input, "attention_mask", mask,
                     "token_type_ids", types))) {
            float[][][] values = (float[][][]) output.get(0).getValue();
            if (values.length != 1 || values[0].length != tokens.length || values[0][0].length != 512) {
                throw new IllegalStateException("Unexpected BGE output tensor");
            }
            return values[0][0];
        }
    }
}
